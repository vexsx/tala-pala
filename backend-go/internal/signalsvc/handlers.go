// Package signalsvc serves trading-signal read endpoints.
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

const signalCols = `id, generated_at, signal, score, confidence, explanation,
	supporting, conflicting, risks, invalidation, review_at, data_fresh, inputs`

func scanSignal(row pgx.Row) (signalRow, error) {
	var s signalRow
	err := row.Scan(&s.ID, &s.GeneratedAt, &s.Signal, &s.Score, &s.Confidence,
		&s.Explanation, &s.Supporting, &s.Conflicting, &s.Risks,
		&s.Invalidation, &s.ReviewAt, &s.DataFresh, &s.Inputs)
	s.GeneratedAt = s.GeneratedAt.UTC()
	return s, err
}

// Current implements GET /api/v1/signals/current.
func (h *Handler) Current(w http.ResponseWriter, r *http.Request) {
	row := h.Pool.QueryRow(r.Context(),
		`SELECT `+signalCols+` FROM signals ORDER BY generated_at DESC LIMIT 1`)
	s, err := scanSignal(row)
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.NotFound(w, "no signals generated yet")
		return
	}
	if err != nil {
		h.Log.Error("signals_current", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, s)
}

// History implements GET /api/v1/signals/history?limit=50.
func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 500 {
			limit = n
		}
	}
	rows, err := h.Pool.Query(r.Context(),
		`SELECT `+signalCols+` FROM signals ORDER BY generated_at DESC LIMIT $1`, limit)
	if err != nil {
		h.Log.Error("signals_history", "error", err)
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
