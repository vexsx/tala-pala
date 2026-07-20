// Package models serves model-registry read endpoints.
package models

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/models*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

type modelVersion struct {
	ID              int             `json:"id"`
	Horizon         string          `json:"horizon"`
	ModelName       string          `json:"model_name"`
	Version         string          `json:"version"`
	TrainedAt       time.Time       `json:"trained_at"`
	TrainingStart   *time.Time      `json:"training_start"`
	TrainingEnd     *time.Time      `json:"training_end"`
	NObservations   *int            `json:"n_observations"`
	Metrics         json.RawMessage `json:"metrics"`
	BaselineMetrics json.RawMessage `json:"baseline_metrics"`
	Params          json.RawMessage `json:"params"`
	IsActive        bool            `json:"is_active"`
}

const mvCols = `id, horizon, model_name, version, trained_at, training_start,
	training_end, n_observations, metrics, baseline_metrics, params, is_active`

func scanModelVersion(row pgx.Row) (modelVersion, error) {
	var m modelVersion
	err := row.Scan(&m.ID, &m.Horizon, &m.ModelName, &m.Version, &m.TrainedAt,
		&m.TrainingStart, &m.TrainingEnd, &m.NObservations, &m.Metrics,
		&m.BaselineMetrics, &m.Params, &m.IsActive)
	m.TrainedAt = m.TrainedAt.UTC()
	return m, err
}

// List implements GET /api/v1/models — all active versions plus recent ones.
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	rows, err := h.Pool.Query(r.Context(), `
		(SELECT `+mvCols+` FROM model_versions WHERE is_active)
		UNION
		(SELECT `+mvCols+` FROM model_versions ORDER BY trained_at DESC LIMIT 30)
		ORDER BY is_active DESC, trained_at DESC`)
	if err != nil {
		h.Log.Error("models_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []modelVersion{}
	for rows.Next() {
		m, err := scanModelVersion(rows)
		if err != nil {
			h.Log.Error("models_list_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, m)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("models_list_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"items": items})
}

// Performance implements GET /api/v1/models/performance: per-horizon active
// model metrics vs baseline, live accuracy from matured predictions, and the
// most recent training run.
func (h *Handler) Performance(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// Active model per horizon.
	rows, err := h.Pool.Query(ctx, `
		SELECT `+mvCols+` FROM model_versions WHERE is_active ORDER BY horizon`)
	if err != nil {
		h.Log.Error("models_perf_active", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	active := map[string]modelVersion{}
	for rows.Next() {
		m, err := scanModelVersion(rows)
		if err != nil {
			rows.Close()
			h.Log.Error("models_perf_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		active[m.Horizon] = m
	}
	rows.Close()

	// Live accuracy from matured predictions, per horizon.
	liveRows, err := h.Pool.Query(ctx, `
		SELECT horizon,
		       count(*) AS n,
		       avg(abs(actual_value - point_forecast) / NULLIF(actual_value, 0) * 100)::float8 AS mape,
		       avg(CASE
		             WHEN (direction = 'up'   AND actual_value > point_forecast / (1 + expected_change_pct/100.0))
		               OR (direction = 'down' AND actual_value < point_forecast / (1 + expected_change_pct/100.0))
		               OR (direction = 'flat')
		             THEN 1.0 ELSE 0.0 END)::float8 AS directional_accuracy,
		       avg(CASE WHEN actual_value BETWEEN lower_bound AND upper_bound THEN 1.0 ELSE 0.0 END)::float8 AS interval_coverage
		FROM predictions
		WHERE actual_value IS NOT NULL
		GROUP BY horizon`)
	if err != nil {
		h.Log.Error("models_perf_live", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	live := map[string]map[string]any{}
	for liveRows.Next() {
		var horizon string
		var n int
		var mape, dirAcc, coverage *float64
		if err := liveRows.Scan(&horizon, &n, &mape, &dirAcc, &coverage); err != nil {
			liveRows.Close()
			h.Log.Error("models_perf_live_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		live[horizon] = map[string]any{
			"n_matured": n, "mape_pct": mape,
			"directional_accuracy": dirAcc, "interval_coverage": coverage,
		}
	}
	liveRows.Close()

	horizons := map[string]any{}
	for hz, m := range active {
		horizons[hz] = map[string]any{
			"model":            m,
			"metrics":          m.Metrics,
			"baseline_metrics": m.BaselineMetrics,
			"live":             live[hz],
		}
	}
	// Include live stats for horizons without an active model.
	for hz, l := range live {
		if _, ok := horizons[hz]; !ok {
			horizons[hz] = map[string]any{"model": nil, "live": l}
		}
	}

	// Last training run.
	var run struct {
		ID              int             `json:"id"`
		StartedAt       time.Time       `json:"started_at"`
		FinishedAt      *time.Time      `json:"finished_at"`
		Status          string          `json:"status"`
		Horizons        []string        `json:"horizons"`
		ModelsEvaluated json.RawMessage `json:"models_evaluated"`
		Selected        json.RawMessage `json:"selected"`
		Error           *string         `json:"error"`
	}
	err = h.Pool.QueryRow(ctx, `
		SELECT id, started_at, finished_at, status, horizons, models_evaluated, selected, error
		FROM training_runs ORDER BY started_at DESC LIMIT 1`).
		Scan(&run.ID, &run.StartedAt, &run.FinishedAt, &run.Status, &run.Horizons,
			&run.ModelsEvaluated, &run.Selected, &run.Error)
	var lastRun any
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		lastRun = nil
	case err != nil:
		h.Log.Error("models_perf_run", "error", err)
		lastRun = nil
	default:
		run.StartedAt = run.StartedAt.UTC()
		lastRun = run
	}

	httpserver.JSON(w, http.StatusOK, map[string]any{
		"horizons":          horizons,
		"last_training_run": lastRun,
	})
}
