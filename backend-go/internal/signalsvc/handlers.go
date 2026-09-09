// Package signalsvc serves trading-signal read endpoints.
//
// Go serves projections of what Python wrote. Nothing in this package scores,
// forecasts, weights a factor or resolves a cost: prediction-python owns the
// signal engine and writes the `signals` rows, and this package renders them.
//
// Three endpoints:
//
//	/signals/current?symbol=   one reading, the shape the existing Overview,
//	                           Brief and ActionPlanner already consume
//	/signals/history?symbol=   that symbol's readings, newest first
//	/signals/overview          the latest reading per eligible symbol, plus an
//	                           explicit statement of what is NOT covered
//	                           (see overview.go)
package signalsvc

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/signals/*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

// signalRow is the /signals/current and /signals/history item.
//
// FROZEN SHAPE. Overview, Brief and ActionPlanner consume this payload today,
// and adding ?symbol= must not change a byte of what they receive. So the
// multi-asset work adds no field here -- not even `symbol`, which the caller
// already knows because it is the parameter it sent (or the documented
// IR_GOLD_18K default). The richer per-asset contract -- evidence basis,
// omitted factors, cost provenance -- lives on /signals/overview, where it can
// be introduced without touching a page that already works.
type signalRow struct {
	ID           int64           `json:"id"`
	GeneratedAt  time.Time       `json:"generated_at"`
	Signal       string          `json:"signal"`
	Score        int             `json:"score"`
	Confidence   float64         `json:"confidence"`
	Explanation  string          `json:"explanation"`
	Supporting   json.RawMessage `json:"supporting"`
	Conflicting  json.RawMessage `json:"conflicting"`
	Risks        json.RawMessage `json:"risks"`
	Invalidation string          `json:"invalidation"`
	ReviewAt     *time.Time      `json:"review_at"`
	DataFresh    bool            `json:"data_fresh"`
	Inputs       json.RawMessage `json:"inputs"`
}

// signalCols is the SELECT list behind signalRow, unchanged by the multi-asset
// work: the same columns in the same order, so the gold reading serialises
// exactly as it does today. `symbol` is filtered on, never selected -- see the
// frozen-shape note above.
const signalCols = `id, generated_at, signal, score, confidence, explanation,
	supporting, conflicting, risks, invalidation, review_at, data_fresh, inputs`

// The two statements behind the frozen shape. Both filter on symbol and
// neither selects it: kept as constants so a test can assert the filter is
// present without a database, because an unfiltered "latest row" is precisely
// the bug migration 0026 makes possible.
const (
	currentSignalSelect = `SELECT ` + signalCols + `
	  FROM signals WHERE symbol = $1
	 ORDER BY generated_at DESC LIMIT 1`

	historySignalSelect = `SELECT ` + signalCols + `
	  FROM signals WHERE symbol = $2
	 ORDER BY generated_at DESC LIMIT $1`
)

func scanSignal(row pgx.Row) (signalRow, error) {
	var s signalRow
	err := row.Scan(&s.ID, &s.GeneratedAt, &s.Signal, &s.Score, &s.Confidence,
		&s.Explanation, &s.Supporting, &s.Conflicting, &s.Risks,
		&s.Invalidation, &s.ReviewAt, &s.DataFresh, &s.Inputs)
	s.GeneratedAt = s.GeneratedAt.UTC()
	return s, err
}

// Current implements GET /api/v1/signals/current?symbol=<code>.
//
// An absent ?symbol= means IR_GOLD_18K, which is what every existing caller
// sends. Before migration 0026 the statement had no WHERE clause at all and the
// table held only gold rows; with other symbols now writing into it, an
// unfiltered "latest row" would hand a caller asking about Tehran gold whichever
// symbol the generator happened to score last.
func (h *Handler) Current(w http.ResponseWriter, r *http.Request) {
	raw := r.URL.Query().Get("symbol")
	symbol, verdict, reason := ParseSignalSymbol(raw)
	if !h.refuseSymbol(w, r, raw, verdict, reason) {
		return
	}

	row := h.Pool.QueryRow(r.Context(), currentSignalSelect, symbol)
	s, err := scanSignal(row)
	if errors.Is(err, pgx.ErrNoRows) {
		// 404, not an empty 200: "this symbol has no reading" is a different
		// answer from "here is a reading", and only one of them is a signal.
		httpserver.NotFound(w, "no signals generated yet for "+symbol)
		return
	}
	if err != nil {
		h.Log.Error("signals_current", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, s)
}

// History implements GET /api/v1/signals/history?symbol=&limit=50.
//
// ?symbol= defaults to IR_GOLD_18K for the same reason /current does: an
// unfiltered history would interleave assets into one list whose items do not
// say which symbol they belong to.
//
// ?limit= keeps its existing lenient parse (an unusable value falls back to 50)
// deliberately. Tightening it to a 400 is a real improvement and a real API
// break, and bundling an unrequested break into the multi-asset change is
// exactly the kind of thing that makes a release hard to reason about.
func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	raw := r.URL.Query().Get("symbol")
	symbol, verdict, reason := ParseSignalSymbol(raw)
	if !h.refuseSymbol(w, r, raw, verdict, reason) {
		return
	}

	limit := 50
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 500 {
			limit = n
		}
	}
	rows, err := h.Pool.Query(r.Context(), historySignalSelect, limit, symbol)
	if err != nil {
		h.Log.Error("signals_history", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []signalRow{}
	for rows.Next() {
		s, err := scanSignal(rows)
		if err != nil {
			h.Log.Error("signals_history_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, s)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("signals_history_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"items": items})
}
