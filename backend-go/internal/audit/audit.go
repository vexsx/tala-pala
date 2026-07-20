// Package audit writes audit log entries.
package audit

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Logger records security-relevant actions in the audit_logs table.
// A nil user id is stored as NULL.
type Logger struct {
	pool *pgxpool.Pool
	log  *slog.Logger
}

// New creates an audit logger backed by the given pool.
func New(pool *pgxpool.Pool, log *slog.Logger) *Logger {
	return &Logger{pool: pool, log: log}
}

// Entry describes one audit record.
type Entry struct {
	UserID    *string // uuid string; nil for anonymous actions
	Action    string  // e.g. 'auth.login'
	Entity    string
	EntityID  string
	Details   map[string]any
	IP        string
	RequestID string
}

// Record inserts the entry; failures are logged, never fatal (auditing must
// not break the request path).
func (l *Logger) Record(ctx context.Context, e Entry) {
	details := e.Details
	if details == nil {
		details = map[string]any{}
	}
	dj, err := json.Marshal(details)
	if err != nil {
		dj = []byte("{}")
	}
	_, err = l.pool.Exec(ctx,
		`INSERT INTO audit_logs (user_id, action, entity, entity_id, details, ip, request_id)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)`,
		e.UserID, e.Action, e.Entity, e.EntityID, dj, e.IP, e.RequestID)
	if err != nil {
		l.log.Error("audit_write_failed", slog.String("action", e.Action), slog.String("error", err.Error()))
	}
}
