package scheduler

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/config"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/internalclient"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
)

// captureHandler keeps every emitted record so tests can assert the level the
// Issues tee handler keys on (WARN+).
type captureHandler struct {
	mu      sync.Mutex
	records []slog.Record
}

func (h *captureHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h *captureHandler) Handle(_ context.Context, rec slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, rec.Clone())
	return nil
}

func (h *captureHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *captureHandler) WithGroup(string) slog.Handler      { return h }

func (h *captureHandler) all() []slog.Record {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]slog.Record(nil), h.records...)
}

// jobFailures reads goldpred_job_failure_total{job=<job>} out of the registry.
func jobFailures(t *testing.T, m *obs.Metrics, job string) float64 {
	t.Helper()
	families, err := m.Registry.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}
	for _, family := range families {
		if family.GetName() != "goldpred_job_failure_total" {
			continue
		}
		for _, metric := range family.GetMetric() {
			for _, label := range metric.GetLabel() {
				if label.GetName() == "job" && label.GetValue() == job {
					return metric.GetCounter().GetValue()
				}
			}
		}
	}
	return 0
}

func attrValue(rec slog.Record, key string) string {
	var found string
	rec.Attrs(func(a slog.Attr) bool {
		if a.Key == key {
			found = a.Value.String()
			return false
		}
		return true
	})
	return found
}

func TestNoteMissedRunRecordsFailureAndLogsAboveInfo(t *testing.T) {
	cases := []struct {
		name      string
		job       string
		reason    string
		err       error
		wantLevel slog.Level
		wantMsg   string
	}{
		{"redis unreachable", "collect", "lock_error", errors.New("dial tcp: connection refused"),
			slog.LevelError, "job_lock_error"},
		{"lock still held", "train", "lock_held", nil,
			slog.LevelWarn, "job_lock_held_elsewhere"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			handler := &captureHandler{}
			metrics := obs.NewMetrics()
			s := &Scheduler{metrics: metrics, log: slog.New(handler)}

			s.noteMissedRun(tc.job, tc.reason, tc.err)

			if got := jobFailures(t, metrics, tc.job); got != 1 {
				t.Fatalf("job_failure_total{job=%q} = %v, want 1", tc.job, got)
			}
			records := handler.all()
			if len(records) != 1 {
				t.Fatalf("emitted %d records, want 1", len(records))
			}
			if records[0].Level != tc.wantLevel {
				t.Fatalf("level = %v, want %v", records[0].Level, tc.wantLevel)
			}
			if records[0].Level < slog.LevelWarn {
				t.Fatal("a missed run must log at WARN or above to reach app_issues")
			}
			if records[0].Message != tc.wantMsg {
				t.Fatalf("message = %q, want %q", records[0].Message, tc.wantMsg)
			}
			if got := attrValue(records[0], "job"); got != tc.job {
				t.Fatalf("job attr = %q, want %q", got, tc.job)
			}
			if got := attrValue(records[0], "reason"); got != tc.reason {
				t.Fatalf("reason attr = %q, want %q", got, tc.reason)
			}
		})
	}
}

func TestRunWithLockRecordsMissedRunWhenRedisIsDown(t *testing.T) {
	rdb := redis.NewClient(&redis.Options{
		Addr:       "redis:6379",
		MaxRetries: -1, // fail on the first attempt instead of sleeping through backoff
		Dialer: func(context.Context, string, string) (net.Conn, error) {
			return nil, errors.New("connection refused")
		},
	})
	defer func() { _ = rdb.Close() }()

	handler := &captureHandler{}
	metrics := obs.NewMetrics()
	s := &Scheduler{redis: rdb, metrics: metrics, log: slog.New(handler), instanceID: "test-instance"}

	ran := false
	s.runWithLock("collect", time.Second, func(context.Context) error {
		ran = true
		return nil
	})

	if ran {
		t.Fatal("the job must not run when the lock could not be acquired")
	}
	if got := jobFailures(t, metrics, "collect"); got != 1 {
		t.Fatalf("job_failure_total{job=\"collect\"} = %v, want 1", got)
	}
	records := handler.all()
	if len(records) != 1 || records[0].Message != "job_lock_error" {
		t.Fatalf("records = %+v, want a single job_lock_error", records)
	}
	if records[0].Level < slog.LevelWarn {
		t.Fatalf("level = %v, want WARN or above", records[0].Level)
	}
}

// The news job must exist as its OWN cron entry. Folding news collection into
// the collect job would let a failing feed delay or fail price collection,
// which is the one thing news must never do.
func TestNewsJobIsRegisteredSeparatelyWithItsOwnTimeout(t *testing.T) {
	cfg := &config.Config{}
	cfg.Crons.Collect = "*/10 * * * *"
	cfg.Crons.Predict = "5 * * * *"
	cfg.Crons.Signals = "10 * * * *"
	cfg.Crons.Evaluate = "20 * * * *"
	cfg.Crons.Train = "30 2 * * *"
	cfg.Crons.Alerts = "*/5 * * * *"
	cfg.Crons.Cleanup = "0 4 * * *"
	cfg.Crons.News = "*/15 * * * *"

	s, err := New(cfg, nil, nil, nil, obs.NewMetrics(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// One entry per job: collect, predict, signals, evaluate, train, alerts,
	// cleanup, news.
	if got := len(s.cron.Entries()); got != 8 {
		t.Fatalf("registered %d cron entries, want 8 (news missing?)", got)
	}
	if internalclient.NewsTimeout <= 0 {
		t.Fatal("news job must declare a positive timeout")
	}
	// GDELT spaces requests >=5s apart and retries inside that budget, so the
	// news job needs more headroom than the 60s per-call default.
	if internalclient.NewsTimeout < time.Minute {
		t.Fatalf("NewsTimeout %v is too short for a throttled multi-query pass", internalclient.NewsTimeout)
	}
}
