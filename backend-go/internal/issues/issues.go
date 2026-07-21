// Package issues mirrors WARN/ERROR logs into the shared app_issues table
// and serves the Issues tab API. One aggregated, exportable view of
// everything going wrong across the Go API, the Python prediction service
// and the frontend.
package issues

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	maxMessageChars = 2000
	queueSize       = 256
	insertTimeout   = 5 * time.Second
)

// Issue is one row bound for app_issues.
type Issue struct {
	OccurredAt time.Time
	Service    string // 'api' | 'prediction' | 'frontend'
	Level      string // 'warning' | 'error'
	Source     string
	Message    string
	Details    map[string]any
}

// Recorder writes issues asynchronously: a bounded queue plus one writer
// goroutine. When the queue is full the issue is dropped — logging must never
// block or break request handling.
type Recorder struct {
	queue chan Issue
	done  chan struct{}
}

// NewRecorder starts the background writer.
func NewRecorder(pool *pgxpool.Pool) *Recorder {
	r := &Recorder{queue: make(chan Issue, queueSize), done: make(chan struct{})}
	go func() {
		defer close(r.done)
		for issue := range r.queue {
			r.insert(pool, issue)
		}
	}()
	return r
}

func (r *Recorder) insert(pool *pgxpool.Pool, issue Issue) {
	ctx, cancel := context.WithTimeout(context.Background(), insertTimeout)
	defer cancel()
	details, err := json.Marshal(issue.Details)
	if err != nil || details == nil {
		details = []byte("{}")
	}
	msg := issue.Message
	if len(msg) > maxMessageChars {
		msg = msg[:maxMessageChars]
	}
	// Failures are intentionally ignored: the record is still on stdout.
	_, _ = pool.Exec(ctx, `
		INSERT INTO app_issues (occurred_at, service, level, source, message, details)
		VALUES ($1, $2, $3, $4, $5, $6)`,
		issue.OccurredAt, issue.Service, issue.Level, issue.Source, msg, details)
}

// Record enqueues an issue, dropping it when the queue is saturated.
func (r *Recorder) Record(issue Issue) {
	if issue.OccurredAt.IsZero() {
		issue.OccurredAt = time.Now().UTC()
	}
	select {
	case r.queue <- issue:
	default: // saturated: drop rather than block
	}
}

// Close stops the writer after draining the queue.
func (r *Recorder) Close() {
	close(r.queue)
	<-r.done
}

// TeeHandler is a slog.Handler that forwards every record to the wrapped
// handler and additionally mirrors Warn/Error records into the Recorder.
type TeeHandler struct {
	inner    slog.Handler
	recorder *Recorder
	attrs    []slog.Attr
}

// NewTeeHandler wraps inner so WARN+ records are also persisted.
func NewTeeHandler(inner slog.Handler, recorder *Recorder) *TeeHandler {
	return &TeeHandler{inner: inner, recorder: recorder}
}

func (h *TeeHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.inner.Enabled(ctx, level)
}

func (h *TeeHandler) Handle(ctx context.Context, rec slog.Record) error {
	if rec.Level >= slog.LevelWarn {
		details := make(map[string]any, rec.NumAttrs()+len(h.attrs))
		for _, a := range h.attrs {
			details[a.Key] = a.Value.String()
		}
		rec.Attrs(func(a slog.Attr) bool {
			details[a.Key] = a.Value.String()
			return true
		})
		level := "warning"
		if rec.Level >= slog.LevelError {
			level = "error"
		}
		source := ""
		if v, ok := details["job"]; ok {
			source, _ = v.(string)
		}
		h.recorder.Record(Issue{
			OccurredAt: rec.Time.UTC(),
			Service:    "api",
			Level:      level,
			Source:     source,
			Message:    rec.Message,
			Details:    details,
		})
	}
	return h.inner.Handle(ctx, rec)
}

func (h *TeeHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	merged := make([]slog.Attr, 0, len(h.attrs)+len(attrs))
	merged = append(merged, h.attrs...)
	merged = append(merged, attrs...)
	return &TeeHandler{inner: h.inner.WithAttrs(attrs), recorder: h.recorder, attrs: merged}
}

func (h *TeeHandler) WithGroup(name string) slog.Handler {
	return &TeeHandler{inner: h.inner.WithGroup(name), recorder: h.recorder, attrs: h.attrs}
}
