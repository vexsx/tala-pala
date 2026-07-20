package alerts

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/alerts*.
type Handler struct {
	Pool  *pgxpool.Pool
	Audit *audit.Logger
	Log   *slog.Logger
}

type alertDTO struct {
	ID              int64           `json:"id"`
	AlertType       string          `json:"alert_type"`
	Condition       json.RawMessage `json:"condition"`
	Enabled         bool            `json:"enabled"`
	CooldownMinutes int             `json:"cooldown_minutes"`
	LastTriggeredAt *time.Time      `json:"last_triggered_at"`
	CreatedAt       time.Time       `json:"created_at"`
}

type alertRequest struct {
	AlertType       string          `json:"alert_type"`
	Condition       json.RawMessage `json:"condition"`
	Enabled         *bool           `json:"enabled"`
	CooldownMinutes *int            `json:"cooldown_minutes"`
}

// ValidateAlertRequest is the pure validation for create/update payloads.
func ValidateAlertRequest(r alertRequest) map[string]any {
	problems := map[string]any{}
	if !AlertTypes[r.AlertType] {
		problems["alert_type"] = "unknown alert type"
	}
	if len(r.Condition) > 0 {
		var m map[string]any
		if err := json.Unmarshal(r.Condition, &m); err != nil {
			problems["condition"] = "must be a JSON object"
		}
	}
	if r.CooldownMinutes != nil && (*r.CooldownMinutes < 1 || *r.CooldownMinutes > 10080) {
		problems["cooldown_minutes"] = "must be between 1 and 10080"
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

// List implements GET /api/v1/alerts.
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	rows, err := h.Pool.Query(r.Context(), `
		SELECT id, alert_type, condition, enabled, cooldown_minutes, last_triggered_at, created_at
		FROM alerts WHERE user_id = $1 ORDER BY id ASC`, u.ID)
	if err != nil {
		h.Log.Error("alerts_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []alertDTO{}
	for rows.Next() {
		var a alertDTO
		if err := rows.Scan(&a.ID, &a.AlertType, &a.Condition, &a.Enabled,
			&a.CooldownMinutes, &a.LastTriggeredAt, &a.CreatedAt); err != nil {
			h.Log.Error("alerts_list_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		a.CreatedAt = a.CreatedAt.UTC()
		items = append(items, a)
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"items": items})
}

// Create implements POST /api/v1/alerts.
func (h *Handler) Create(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	var req alertRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	if problems := ValidateAlertRequest(req); problems != nil {
		httpserver.BadRequest(w, "invalid alert", problems)
		return
	}
	cond := req.Condition
	if len(cond) == 0 {
		cond = json.RawMessage("{}")
	}
	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	cooldown := 60
	if req.CooldownMinutes != nil {
		cooldown = *req.CooldownMinutes
	}
	var id int64
	err := h.Pool.QueryRow(r.Context(), `
		INSERT INTO alerts (user_id, alert_type, condition, enabled, cooldown_minutes)
		VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		u.ID, req.AlertType, cond, enabled, cooldown).Scan(&id)
	if err != nil {
		h.Log.Error("alerts_create", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "alert.create", Entity: "alert",
		EntityID:  strconv.FormatInt(id, 10),
		Details:   map[string]any{"alert_type": req.AlertType},
		RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusCreated, map[string]any{"id": id})
}

// Update implements PUT /api/v1/alerts/{id}.
func (h *Handler) Update(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid alert id", nil)
		return
	}
	var req alertRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	if problems := ValidateAlertRequest(req); problems != nil {
		httpserver.BadRequest(w, "invalid alert", problems)
		return
	}
	cond := req.Condition
	if len(cond) == 0 {
		cond = json.RawMessage("{}")
	}
	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	cooldown := 60
	if req.CooldownMinutes != nil {
		cooldown = *req.CooldownMinutes
	}
	tag, err := h.Pool.Exec(r.Context(), `
		UPDATE alerts
		SET alert_type=$1, condition=$2, enabled=$3, cooldown_minutes=$4, updated_at=now()
		WHERE id=$5 AND user_id=$6`,
		req.AlertType, cond, enabled, cooldown, id, u.ID)
	if err != nil {
		h.Log.Error("alerts_update", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "alert not found")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "alert.update", Entity: "alert",
		EntityID: strconv.FormatInt(id, 10), RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "updated": true})
}

// Delete implements DELETE /api/v1/alerts/{id}.
func (h *Handler) Delete(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid alert id", nil)
		return
	}
	tag, err := h.Pool.Exec(r.Context(),
		`DELETE FROM alerts WHERE id=$1 AND user_id=$2`, id, u.ID)
	if err != nil {
		h.Log.Error("alerts_delete", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "alert not found")
		return
	}
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &u.ID, Action: "alert.delete", Entity: "alert",
		EntityID: strconv.FormatInt(id, 10), RequestID: middleware.GetReqID(r.Context()),
	})
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "deleted": true})
}

// Events implements GET /api/v1/alerts/events?unacked=true.
func (h *Handler) Events(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	unackedOnly := r.URL.Query().Get("unacked") == "true"
	rows, err := h.Pool.Query(r.Context(), `
		SELECT e.id, e.alert_id, a.alert_type, e.triggered_at, e.message, e.payload, e.acknowledged
		FROM alert_events e
		JOIN alerts a ON a.id = e.alert_id
		WHERE e.user_id = $1 AND ($2 = false OR e.acknowledged = false)
		ORDER BY e.triggered_at DESC LIMIT 200`, u.ID, unackedOnly)
	if err != nil {
		h.Log.Error("alerts_events", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	type eventDTO struct {
		ID           int64           `json:"id"`
		AlertID      int64           `json:"alert_id"`
		AlertType    string          `json:"alert_type"`
		TriggeredAt  time.Time       `json:"triggered_at"`
		Message      string          `json:"message"`
		Payload      json.RawMessage `json:"payload"`
		Acknowledged bool            `json:"acknowledged"`
	}
	items := []eventDTO{}
	for rows.Next() {
		var e eventDTO
		if err := rows.Scan(&e.ID, &e.AlertID, &e.AlertType, &e.TriggeredAt,
			&e.Message, &e.Payload, &e.Acknowledged); err != nil {
			h.Log.Error("alerts_events_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		e.TriggeredAt = e.TriggeredAt.UTC()
		items = append(items, e)
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"items": items})
}

// AckEvent implements POST /api/v1/alerts/events/{id}/ack.
func (h *Handler) AckEvent(w http.ResponseWriter, r *http.Request) {
	u, ok := mustUser(w, r)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid event id", nil)
		return
	}
	tag, err := h.Pool.Exec(r.Context(),
		`UPDATE alert_events SET acknowledged = true WHERE id=$1 AND user_id=$2`, id, u.ID)
	if err != nil {
		h.Log.Error("alerts_ack", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "event not found")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "acknowledged": true})
}
