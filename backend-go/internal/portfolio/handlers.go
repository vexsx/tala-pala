package portfolio

import (
	"encoding/csv"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/portfolio*.
type Handler struct {
	Pool  *pgxpool.Pool
	Audit *audit.Logger
	Log   *slog.Logger
}

type transaction struct {
	ID           int64     `json:"id"`
	TxType       string    `json:"tx_type"`
	Grams        float64   `json:"grams"`
	Karat        int       `json:"karat"`
	PricePerGram float64   `json:"price_per_gram"`
	Currency     string    `json:"currency"`
	Fees         float64   `json:"fees"`
	TxDate       string    `json:"tx_date"`
	Notes        string    `json:"notes"`
	CreatedAt    time.Time `json:"created_at"`
}

type txRequest struct {
	TxType       string  `json:"tx_type"`
	Grams        float64 `json:"grams"`
	Karat        int     `json:"karat"`
	PricePerGram float64 `json:"price_per_gram"`
	Currency     string  `json:"currency"`
	Fees         float64 `json:"fees"`
	TxDate       string  `json:"tx_date"`
	Notes        string  `json:"notes"`
}

// ValidateTxRequest is the pure validation for create/update payloads.
func ValidateTxRequest(t txRequest) map[string]any {
	problems := map[string]any{}
	if t.TxType != "buy" && t.TxType != "sell" {
		problems["tx_type"] = "must be buy or sell"
	}
	if t.Grams <= 0 {
		problems["grams"] = "must be positive"
	}
	if !validKarat(t.Karat) {
		problems["karat"] = "must be one of 18, 21, 22, 24"
	}
	if t.PricePerGram <= 0 {
		problems["price_per_gram"] = "must be positive"
	}
	if t.Currency != "IRT" && t.Currency != "IRR" {
		problems["currency"] = "must be IRT or IRR"
	}
	if t.Fees < 0 {
		problems["fees"] = "must be non-negative"
	}
	if _, err := time.Parse("2006-01-02", t.TxDate); err != nil {
		problems["tx_date"] = "must be YYYY-MM-DD"
	}
	if len(t.Notes) > 1000 {
		problems["notes"] = "too long (max 1000 chars)"
	}
	if len(problems) == 0 {
		return nil
	}
	return problems
}

func mustUser(w http.ResponseWriter, r *http.Request) (httpserver.AuthUser, bool) {
	u, ok := httpserver.UserFromContext(r.Context())
	if !ok {
		httpserver.Unauthorized(w, "not authenticated")
	}
	return u, ok
}

func (h *Handler) userTransactions(r *http.Request, userID string) ([]transaction, []Tx, error) {
	rows, err := h.Pool.Query(r.Context(), `
		SELECT id, tx_type, grams::float8, karat, price_per_gram::float8,
		       currency, fees::float8, to_char(tx_date, 'YYYY-MM-DD'), notes, created_at
		FROM portfolio_transactions
		WHERE user_id = $1
		ORDER BY tx_date ASC, id ASC`, userID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var list []transaction
	var calc []Tx
	for rows.Next() {
		var t transaction
		if err := rows.Scan(&t.ID, &t.TxType, &t.Grams, &t.Karat, &t.PricePerGram,
			&t.Currency, &t.Fees, &t.TxDate, &t.Notes, &t.CreatedAt); err != nil {
			return nil, nil, err
		}
		t.CreatedAt = t.CreatedAt.UTC()
		list = append(list, t)
		calc = append(calc, Tx{
			Type: t.TxType, Grams: t.Grams, Karat: t.Karat,
			PricePerGram: t.PricePerGram, Fees: t.Fees, Currency: t.Currency,
		})
	}
	return list, calc, rows.Err()
}

func (h *Handler) current18k(r *http.Request) (float64, bool) {
	var v float64
	err := h.Pool.QueryRow(r.Context(), `
		SELECT value::float8 FROM prices
		WHERE symbol = 'IR_GOLD_18K' AND quality = 'ok'
		ORDER BY observed_at DESC LIMIT 1`).Scan(&v)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false
	}
	if err != nil {
		return 0, false
	}
	return v, true
}

// Get implements GET /api/v1/portfolio.
func (h *Handler) Get(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	list, calc, err := h.userTransactions(r, u.ID)
	if err != nil {
		h.Log.Error("portfolio_get", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	price, hasPrice := h.current18k(r)
	s := Compute(calc, price, 10)
	if list == nil {
		list = []transaction{}
	}
	// Flat shape expected by the frontend: holdings + computed fields inline.
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"holdings":                    list,
		"total_grams_18k_equivalent":  s.TotalGrams18kEquivalent,
		"invested":                    s.Invested,
		"current_value":               s.CurrentValue,
		"unrealized_pnl":              s.UnrealizedPnL,
		"pnl_pct":                     s.PnLPct,
		"avg_price":                   s.AvgPrice,
		"break_even_price":            s.BreakEvenPrice,
		"scenarios":                   s.Scenarios,
		"target_price_for_profit_pct": s.TargetPriceForProfitPct,
		"target_profit_pct":           s.TargetProfitPct,
		"current_price":               price,
		"price_available":             hasPrice,
	})
}

// CreateTransaction implements POST /api/v1/portfolio/transactions.
func (h *Handler) CreateTransaction(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	var req txRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	normalizeTxRequest(&req)
	if problems := ValidateTxRequest(req); problems != nil {
		httpserver.BadRequest(w, "invalid transaction", problems)
		return
	}
	var id int64
	err := h.Pool.QueryRow(r.Context(), `
		INSERT INTO portfolio_transactions
			(user_id, tx_type, grams, karat, price_per_gram, currency, fees, tx_date, notes)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id`,
		u.ID, req.TxType, req.Grams, req.Karat, req.PricePerGram,
		req.Currency, req.Fees, req.TxDate, req.Notes).Scan(&id)
	if err != nil {
		h.Log.Error("portfolio_create", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "portfolio.create", Entity: "portfolio_transaction",
		EntityID: strconv.FormatInt(id, 10), RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusCreated, map[string]any{"id": id})
}

// UpdateTransaction implements PUT /api/v1/portfolio/transactions/{id}.
func (h *Handler) UpdateTransaction(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid transaction id", nil)
		return
	}
	var req txRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	normalizeTxRequest(&req)
	if problems := ValidateTxRequest(req); problems != nil {
		httpserver.BadRequest(w, "invalid transaction", problems)
		return
	}
	tag, err := h.Pool.Exec(r.Context(), `
		UPDATE portfolio_transactions
		SET tx_type=$1, grams=$2, karat=$3, price_per_gram=$4, currency=$5,
		    fees=$6, tx_date=$7, notes=$8, updated_at=now()
		WHERE id=$9 AND user_id=$10`,
		req.TxType, req.Grams, req.Karat, req.PricePerGram, req.Currency,
		req.Fees, req.TxDate, req.Notes, id, u.ID)
	if err != nil {
		h.Log.Error("portfolio_update", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "transaction not found")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "portfolio.update", Entity: "portfolio_transaction",
		EntityID: strconv.FormatInt(id, 10), RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "updated": true})
}

// DeleteTransaction implements DELETE /api/v1/portfolio/transactions/{id}.
func (h *Handler) DeleteTransaction(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid transaction id", nil)
		return
	}
	tag, err := h.Pool.Exec(r.Context(),
		`DELETE FROM portfolio_transactions WHERE id=$1 AND user_id=$2`, id, u.ID)
	if err != nil {
		h.Log.Error("portfolio_delete", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "transaction not found")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "portfolio.delete", Entity: "portfolio_transaction",
		EntityID: strconv.FormatInt(id, 10), RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "deleted": true})
}

// Import implements POST /api/v1/portfolio/import (multipart CSV, max 1 MB).
// The import is all-or-nothing: any invalid row rejects the whole file.
func (h *Handler) Import(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, MaxImportBytes+4096) // csv + multipart overhead
	if err := r.ParseMultipartForm(MaxImportBytes); err != nil {
		httpserver.BadRequest(w, "invalid multipart form (max 1MB)", map[string]any{"reason": err.Error()})
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		httpserver.BadRequest(w, `multipart field "file" is required`, nil)
		return
	}
	defer func() { _ = file.Close() }()
	if header.Size > MaxImportBytes {
		httpserver.Error(w, http.StatusRequestEntityTooLarge, "file_too_large", "CSV must be at most 1MB", nil)
		return
	}

	rows, rowErrs := ParseImportCSV(file)
	if len(rowErrs) > 0 {
		httpserver.BadRequest(w, "CSV validation failed", map[string]any{"row_errors": rowErrs})
		return
	}
	if len(rows) == 0 {
		httpserver.BadRequest(w, "CSV contains no data rows", nil)
		return
	}

	ctx := r.Context()
	tx, err := h.Pool.Begin(ctx)
	if err != nil {
		h.Log.Error("portfolio_import_begin", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	for _, row := range rows {
		_, err := tx.Exec(ctx, `
			INSERT INTO portfolio_transactions
				(user_id, tx_type, grams, karat, price_per_gram, currency, fees, tx_date, notes)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
			u.ID, row.Type, row.Grams, row.Karat, row.PricePerGram,
			row.Currency, row.Fees, row.TxDate.Format("2006-01-02"), row.Notes)
		if err != nil {
			h.Log.Error("portfolio_import_insert", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
	}
	if err := tx.Commit(ctx); err != nil {
		h.Log.Error("portfolio_import_commit", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	h.Audit.Record(ctx, audit.Entry{
		UserID: &u.ID, Action: "portfolio.import", Entity: "portfolio_transaction",
		Details:   map[string]any{"rows": len(rows), "filename": header.Filename},
		RequestID: middleware.GetReqID(ctx),
	})
	httpserver.JSON(w, http.StatusCreated, map[string]any{"imported": len(rows)})
}

// Export implements GET /api/v1/portfolio/export (CSV download with
// formula-injection sanitization).
func (h *Handler) Export(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	list, _, err := h.userTransactions(r, u.ID)
	if err != nil {
		h.Log.Error("portfolio_export", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition",
		fmt.Sprintf(`attachment; filename="portfolio-%s.csv"`, time.Now().UTC().Format("2006-01-02")))
	cw := csv.NewWriter(w)
	_ = cw.Write(CSVColumns)
	for _, t := range list {
		_ = cw.Write(ExportRecord(t.TxType, t.Grams, t.Karat, t.PricePerGram,
			t.Currency, t.Fees, t.TxDate, t.Notes))
	}
	cw.Flush()
}

func normalizeTxRequest(t *txRequest) {
	t.TxType = strings.ToLower(strings.TrimSpace(t.TxType))
	t.Currency = strings.ToUpper(strings.TrimSpace(t.Currency))
	if t.Currency == "" {
		t.Currency = "IRT"
	}
	if t.Karat == 0 {
		t.Karat = 18
	}
	t.Notes = strings.TrimSpace(t.Notes)
}
