// Package admin serves admin-only endpoints: job triggers (proxied to the
// Python prediction service) and the audit log.
package admin

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/internalclient"
)

// Handler serves /api/v1/admin/*.
type Handler struct {
	Pool   *pgxpool.Pool
	Client *internalclient.Client
	Audit  *audit.Logger
	Log    *slog.Logger
}

// allowed job names for POST /api/v1/admin/jobs/{job}.
var allowedJobs = map[string]bool{
	"collect": true, "train": true, "predict": true,
	"signals": true, "backtest": true, "evaluate": true,
}

type jobsBody struct {
	Jobs     []string `json:"jobs"`
	Horizons []string `json:"horizons"`
}

// TriggerJob implements POST /api/v1/admin/jobs/{job}, proxying to Python.
func (h *Handler) TriggerJob(w http.ResponseWriter, r *http.Request) {
	job := chi.URLParam(r, "job")
	if !allowedJobs[job] {
		httpserver.BadRequest(w, "unknown job", map[string]any{
			"job": job, "allowed": []string{"collect", "train", "predict", "signals", "backtest", "evaluate"}})
		return
	}
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	var body jobsBody
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &body)
	}

	ctx := r.Context()
	var (
		result json.RawMessage
		err    error
	)
	switch job {
	case "collect":
		result, err = h.Client.Collect(ctx, body.Jobs)
	case "train":
		result, err = h.Client.Train(ctx, body.Horizons)
	case "predict":
		// Regenerate features first so predictions use fresh inputs.
		if _, ferr := h.Client.GenerateFeatures(ctx); ferr != nil {
			h.Log.Warn("admin_predict_features", "error", ferr)
		}
		result, err = h.Client.Predict(ctx, body.Horizons)
	case "signals":
		result, err = h.Client.GenerateSignals(ctx)
	case "backtest":
		result, err = h.Client.Backtest(ctx, raw)
	case "evaluate":
		result, err = h.Client.Evaluate(ctx)
	}

	u, _ := httpserver.UserFromContext(ctx)
	h.Audit.Record(ctx, audit.Entry{
		UserID: &u.ID, Action: "admin.job." + job, Entity: "job", EntityID: job,
		Details:   map[string]any{"success": err == nil},
		RequestID: middleware.GetReqID(ctx),
	})

	if err != nil {
		h.Log.Error("admin_job_failed", "job", job, "error", err)
		httpserver.Error(w, http.StatusBadGateway, "upstream_error",
			"prediction service call failed", map[string]any{"job": job, "reason": err.Error()})
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(result)
}

// AuditList implements GET /api/v1/admin/audit?page=1 (50 per page).
func (h *Handler) AuditList(w http.ResponseWriter, r *http.Request) {
	page := 1
	if v := r.URL.Query().Get("page"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 {
			page = n
		}
	}
	const pageSize = 50
	offset := (page - 1) * pageSize

	ctx := r.Context()
	var total int
	if err := h.Pool.QueryRow(ctx, `SELECT count(*) FROM audit_logs`).Scan(&total); err != nil {
		h.Log.Error("audit_count", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	rows, err := h.Pool.Query(ctx, `
		SELECT id, user_id, action, entity, entity_id, details, ip, request_id, created_at
		FROM audit_logs ORDER BY created_at DESC LIMIT $1 OFFSET $2`, pageSize, offset)
	if err != nil {
		h.Log.Error("audit_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()

	type entryDTO struct {
		ID        int64           `json:"id"`
		UserID    *string         `json:"user_id"`
		Action    string          `json:"action"`
		Entity    string          `json:"entity"`
		EntityID  string          `json:"entity_id"`
		Details   json.RawMessage `json:"details"`
		IP        string          `json:"ip"`
		RequestID string          `json:"request_id"`
		CreatedAt time.Time       `json:"created_at"`
	}
	items := []entryDTO{}
	for rows.Next() {
		var e entryDTO
		if err := rows.Scan(&e.ID, &e.UserID, &e.Action, &e.Entity, &e.EntityID,
			&e.Details, &e.IP, &e.RequestID, &e.CreatedAt); err != nil {
			h.Log.Error("audit_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		e.CreatedAt = e.CreatedAt.UTC()
		items = append(items, e)
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"items": items, "page": page, "page_size": pageSize, "total": total,
	})
}
