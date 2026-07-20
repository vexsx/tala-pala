// Package predictions serves forecast read endpoints.
package predictions

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Horizons is the canonical horizon set.
var Horizons = map[string]bool{
	"1h": true, "4h": true, "eod": true, "1d": true, "3d": true, "7d": true, "30d": true,
}

// Handler serves /api/v1/predictions*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

type prediction struct {
	ID                int64           `json:"id"`
	Symbol            string          `json:"symbol"`
	Horizon           string          `json:"horizon"`
	ModelName         string          `json:"model_name"`
	PredictedAt       time.Time       `json:"predicted_at"`
	TargetTime        time.Time       `json:"target_time"`
	PointForecast     float64         `json:"point_forecast"`
	LowerBound        float64         `json:"lower_bound"`
	UpperBound        float64         `json:"upper_bound"`
	ExpectedChangePct float64         `json:"expected_change_pct"`
	Direction         string          `json:"direction"`
	Confidence        float64         `json:"confidence"`
	Regime            string          `json:"regime"`
	Drivers           json.RawMessage `json:"drivers"`
	DataFresh         bool            `json:"data_fresh"`
	Warnings          json.RawMessage `json:"warnings"`
	ActualValue       *float64        `json:"actual_value"`
	ActualRecordedAt  *time.Time      `json:"actual_recorded_at"`
}

const predictionCols = `id, symbol, horizon, model_name, predicted_at, target_time,
	point_forecast::float8, lower_bound::float8, upper_bound::float8,
	expected_change_pct, direction, confidence, regime, drivers, data_fresh,
	warnings, actual_value::float8, actual_recorded_at`

func scanPrediction(row pgx.Row) (prediction, error) {
	var p prediction
	err := row.Scan(&p.ID, &p.Symbol, &p.Horizon, &p.ModelName, &p.PredictedAt,
		&p.TargetTime, &p.PointForecast, &p.LowerBound, &p.UpperBound,
		&p.ExpectedChangePct, &p.Direction, &p.Confidence, &p.Regime,
		&p.Drivers, &p.DataFresh, &p.Warnings, &p.ActualValue, &p.ActualRecordedAt)
	p.PredictedAt = p.PredictedAt.UTC()
	p.TargetTime = p.TargetTime.UTC()
	return p, err
}

// Latest implements GET /api/v1/predictions — latest prediction per horizon.
func (h *Handler) Latest(w http.ResponseWriter, r *http.Request) {
	rows, err := h.Pool.Query(r.Context(), `
		SELECT DISTINCT ON (horizon) `+predictionCols+`
		FROM predictions
		ORDER BY horizon, predicted_at DESC`)
	if err != nil {
		h.Log.Error("predictions_latest", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []prediction{}
	for rows.Next() {
		p, err := scanPrediction(rows)
		if err != nil {
			h.Log.Error("predictions_latest_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, p)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("predictions_latest_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"items": items,
		"as_of": time.Now().UTC(),
	})
}

// History implements GET /api/v1/predictions/{horizon}?limit=50.
func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	horizon := chi.URLParam(r, "horizon")
	if !Horizons[horizon] {
		httpserver.BadRequest(w, "unknown horizon", map[string]any{"horizon": horizon})
		return
	}
	limit := limitParam(r.URL.Query().Get("limit"), 50, 500)

	rows, err := h.Pool.Query(r.Context(), `
		SELECT `+predictionCols+`
		FROM predictions WHERE horizon = $1
		ORDER BY predicted_at DESC LIMIT $2`, horizon, limit)
	if err != nil {
		h.Log.Error("predictions_history", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []prediction{}
	for rows.Next() {
		p, err := scanPrediction(rows)
		if err != nil {
			h.Log.Error("predictions_history_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, p)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("predictions_history_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"horizon": horizon, "items": items})
}

func limitParam(s string, def, maxV int) int {
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil || n < 1 {
		return def
	}
	if n > maxV {
		return maxV
	}
	return n
}
