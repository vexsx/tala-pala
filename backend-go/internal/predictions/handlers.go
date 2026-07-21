// Package predictions serves forecast read endpoints.
package predictions

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/internalclient"
)

// Horizons is the canonical horizon set.
var Horizons = map[string]bool{
	"1h": true, "4h": true, "eod": true, "1d": true, "3d": true, "7d": true, "30d": true,
}

// Handler serves /api/v1/predictions*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
	// Client reaches the Python prediction service for on-demand custom
	// horizons (may be nil in tests; Custom then responds 503).
	Client *internalclient.Client
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

// ForecastSymbols mirrors the Python FORECAST_SYMBOLS set (Addendum 8).
var ForecastSymbols = map[string]bool{"IR_GOLD_18K": true, "XAUUSD": true}

// Latest implements GET /api/v1/predictions?symbol= — latest per horizon.
func (h *Handler) Latest(w http.ResponseWriter, r *http.Request) {
	symbol := r.URL.Query().Get("symbol")
	if symbol == "" {
		symbol = "IR_GOLD_18K"
	}
	if !ForecastSymbols[symbol] {
		httpserver.BadRequest(w, "unknown forecast symbol", map[string]any{"symbol": symbol})
		return
	}
	rows, err := h.Pool.Query(r.Context(), `
		SELECT DISTINCT ON (horizon) `+predictionCols+`
		FROM predictions
		WHERE symbol = $1
		ORDER BY horizon, predicted_at DESC`, symbol)
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
		"symbol": symbol,
		"items":  items,
		"as_of":  time.Now().UTC(),
	})
}

// History implements GET /api/v1/predictions/{horizon}?symbol=&limit=50.
func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	horizon := chi.URLParam(r, "horizon")
	if !Horizons[horizon] {
		httpserver.BadRequest(w, "unknown horizon", map[string]any{"horizon": horizon})
		return
	}
	symbol := r.URL.Query().Get("symbol")
	if symbol == "" {
		symbol = "IR_GOLD_18K"
	}
	if !ForecastSymbols[symbol] {
		httpserver.BadRequest(w, "unknown forecast symbol", map[string]any{"symbol": symbol})
		return
	}
	limit := limitParam(r.URL.Query().Get("limit"), 50, 500)

	rows, err := h.Pool.Query(r.Context(), `
		SELECT `+predictionCols+`
		FROM predictions WHERE horizon = $1 AND symbol = $3
		ORDER BY predicted_at DESC LIMIT $2`, horizon, limit, symbol)
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

// Custom implements GET /api/v1/predictions/custom?days=N — an on-demand
// forecast for an arbitrary 1-90 day decision horizon, computed live by the
// prediction service (nothing is persisted).
func (h *Handler) Custom(w http.ResponseWriter, r *http.Request) {
	if h.Client == nil {
		httpserver.Error(w, http.StatusServiceUnavailable, "unavailable",
			"prediction service not configured", nil)
		return
	}
	days, err := strconv.Atoi(r.URL.Query().Get("days"))
	if err != nil || days < 1 || days > 90 {
		httpserver.BadRequest(w, "days must be an integer between 1 and 90",
			map[string]any{"days": r.URL.Query().Get("days")})
		return
	}
	payload, err := h.Client.PredictCustom(r.Context(), days)
	if err != nil {
		var apiErr *internalclient.APIError
		if errors.As(err, &apiErr) && apiErr.Status >= 400 && apiErr.Status < 500 {
			// pass the Python error envelope through (e.g. "not enough history")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(apiErr.Status)
			_, _ = w.Write([]byte(apiErr.Body))
			return
		}
		h.Log.Error("predictions_custom", "error", err)
		httpserver.Error(w, http.StatusBadGateway, "upstream_error",
			"prediction service failed to compute the custom forecast", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(payload)
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
