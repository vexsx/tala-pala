package issues

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/issues*.
type Handler struct {
	Pool     *pgxpool.Pool
	Recorder *Recorder
	Log      *slog.Logger
}

type issueRow struct {
	ID         int64           `json:"id"`
	OccurredAt time.Time       `json:"occurred_at"`
	Service    string          `json:"service"`
	Level      string          `json:"level"`
	Source     string          `json:"source"`
	Message    string          `json:"message"`
	Details    json.RawMessage `json:"details"`
}

var validLevels = map[string]bool{"warning": true, "error": true}
var validServices = map[string]bool{"api": true, "prediction": true, "frontend": true}

// List implements GET /api/v1/issues?limit=&level=&service=&since_hours=.
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := clampInt(q.Get("limit"), 100, 1, 1000)
	sinceHours := clampInt(q.Get("since_hours"), 72, 1, 24*30)

	where := []string{"occurred_at >= $1"}
	args := []any{time.Now().UTC().Add(-time.Duration(sinceHours) * time.Hour)}
	if lvl := q.Get("level"); lvl != "" {
		if !validLevels[lvl] {
			httpserver.BadRequest(w, "unknown level", map[string]any{"level": lvl})
			return
		}
		args = append(args, lvl)
		where = append(where, fmt.Sprintf("level = $%d", len(args)))
	}
	if svc := q.Get("service"); svc != "" {
		if !validServices[svc] {
			httpserver.BadRequest(w, "unknown service", map[string]any{"service": svc})
			return
		}
		args = append(args, svc)
		where = append(where, fmt.Sprintf("service = $%d", len(args)))
	}
	args = append(args, limit)

	rows, err := h.Pool.Query(r.Context(), `
		SELECT id, occurred_at, service, level, source, message, details
		FROM app_issues
		WHERE `+strings.Join(where, " AND ")+`
		ORDER BY occurred_at DESC
		LIMIT $`+strconv.Itoa(len(args)), args...)
	if err != nil {
		h.Log.Error("issues_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []issueRow{}
	for rows.Next() {
		var it issueRow
		if err := rows.Scan(&it.ID, &it.OccurredAt, &it.Service, &it.Level,
			&it.Source, &it.Message, &it.Details); err != nil {
			h.Log.Error("issues_list_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		it.OccurredAt = it.OccurredAt.UTC()
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("issues_list_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"items": items, "as_of": time.Now().UTC(),
	})
}

type createRequest struct {
	Level   string         `json:"level"`
	Source  string         `json:"source"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
}

// Create implements POST /api/v1/issues — frontend error reporting. The
// service is forced to 'frontend'; clients cannot impersonate backend rows.
func (h *Handler) Create(w http.ResponseWriter, r *http.Request) {
	var req createRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&req); err != nil {
		httpserver.BadRequest(w, "invalid JSON body", nil)
		return
	}
	if req.Message == "" {
		httpserver.BadRequest(w, "message is required", nil)
		return
	}
	if !validLevels[req.Level] {
		req.Level = "error"
	}
	h.Recorder.Record(Issue{
		Service: "frontend",
		Level:   req.Level,
		Source:  truncate(req.Source, 200),
		Message: req.Message,
		Details: req.Details,
	})
	httpserver.JSON(w, http.StatusAccepted, map[string]any{"status": "accepted"})
}

// Report implements GET /api/v1/issues/report — a self-contained Markdown
// digest (recent issues + provider health + training runs) the user can paste
// into a debugging conversation.
func (h *Handler) Report(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var b strings.Builder
	now := time.Now().UTC()
	fmt.Fprintf(&b, "# Issue report — %s\n\n", now.Format(time.RFC3339))

	// Recent issues (72h, newest first).
	fmt.Fprintf(&b, "## Recent warnings & errors (last 72h)\n\n")
	rows, err := h.Pool.Query(ctx, `
		SELECT occurred_at, service, level, source, message, details
		FROM app_issues WHERE occurred_at >= $1
		ORDER BY occurred_at DESC LIMIT 200`, now.Add(-72*time.Hour))
	if err == nil {
		count := 0
		func() {
			defer rows.Close()
			for rows.Next() {
				var ts time.Time
				var service, level, source, message string
				var details json.RawMessage
				if rows.Scan(&ts, &service, &level, &source, &message, &details) != nil {
					return
				}
				count++
				fmt.Fprintf(&b, "- `%s` **%s/%s**", ts.UTC().Format("2006-01-02 15:04"), service, level)
				if source != "" {
					fmt.Fprintf(&b, " [%s]", source)
				}
				fmt.Fprintf(&b, ": %s\n", message)
				if len(details) > 2 { // more than '{}'
					fmt.Fprintf(&b, "  - details: `%s`\n", truncate(string(details), 800))
				}
			}
		}()
		if count == 0 {
			b.WriteString("(none recorded)\n")
		}
	} else {
		fmt.Fprintf(&b, "(query failed: %v)\n", err)
	}

	// Provider health.
	fmt.Fprintf(&b, "\n## Data providers\n\n")
	prows, err := h.Pool.Query(ctx, `
		SELECT code, enabled, consecutive_failures,
		       COALESCE(last_error, ''), last_success_at, last_error_at
		FROM data_providers ORDER BY priority`)
	if err == nil {
		defer prows.Close()
		for prows.Next() {
			var code, lastErr string
			var enabled bool
			var failures int
			var lastOK, lastErrAt *time.Time
			if prows.Scan(&code, &enabled, &failures, &lastErr, &lastOK, &lastErrAt) != nil {
				break
			}
			status := "ok"
			if !enabled {
				status = "disabled"
			} else if failures > 0 {
				status = fmt.Sprintf("%d consecutive failures", failures)
			}
			fmt.Fprintf(&b, "- **%s**: %s", code, status)
			if lastOK != nil {
				fmt.Fprintf(&b, "; last success %s", lastOK.UTC().Format("2006-01-02 15:04"))
			}
			if lastErr != "" && failures > 0 {
				fmt.Fprintf(&b, "; last error: %s", truncate(lastErr, 300))
			}
			b.WriteString("\n")
		}
	}

	// Training runs.
	fmt.Fprintf(&b, "\n## Recent training runs\n\n")
	trows, err := h.Pool.Query(ctx, `
		SELECT id, started_at, finished_at, status, COALESCE(error, ''), COALESCE(notes, '')
		FROM training_runs ORDER BY started_at DESC LIMIT 5`)
	if err == nil {
		defer trows.Close()
		for trows.Next() {
			var id int64
			var started time.Time
			var finished *time.Time
			var status, trainErr, notes string
			if trows.Scan(&id, &started, &finished, &status, &trainErr, &notes) != nil {
				break
			}
			fmt.Fprintf(&b, "- run %d: **%s** started %s", id, status, started.UTC().Format("2006-01-02 15:04"))
			if trainErr != "" {
				fmt.Fprintf(&b, "\n  - error: %s", truncate(trainErr, 500))
			}
			if notes != "" {
				fmt.Fprintf(&b, "\n  - notes: %s", truncate(notes, 500))
			}
			b.WriteString("\n")
		}
	}

	w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(b.String()))
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

func clampInt(s string, def, minV, maxV int) int {
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	if n < minV {
		return minV
	}
	if n > maxV {
		return maxV
	}
	return n
}
