// Package scheduler runs cron jobs. Every job takes a Redis lock
// (SET NX PX 10m, key lock:job:<name>) so only one API replica runs it.
package scheduler

import (
	"context"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"github.com/robfig/cron/v3"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/alerts"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/config"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/internalclient"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
)

// LockTTL is the Redis lock expiry for each job run.
const LockTTL = 10 * time.Minute

// releaseScript deletes the lock only if this instance still owns it.
const releaseScript = `
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end`

// Scheduler wires cron specs to jobs.
type Scheduler struct {
	cron       *cron.Cron
	redis      *redis.Client
	client     *internalclient.Client
	alerts     *alerts.Runner
	metrics    *obs.Metrics
	log        *slog.Logger
	instanceID string
}

// New builds the scheduler with all jobs registered from the cron config.
func New(cfg *config.Config, rdb *redis.Client, client *internalclient.Client,
	alertRunner *alerts.Runner, metrics *obs.Metrics, log *slog.Logger) (*Scheduler, error) {

	s := &Scheduler{
		cron:       cron.New(cron.WithLocation(time.UTC)),
		redis:      rdb,
		client:     client,
		alerts:     alertRunner,
		metrics:    metrics,
		log:        log,
		instanceID: uuid.NewString(),
	}

	jobs := []struct {
		name string
		spec string
		fn   func(ctx context.Context) error
	}{
		{"collect", cfg.Crons.Collect, func(ctx context.Context) error {
			_, err := client.Collect(ctx, nil)
			return err
		}},
		{"predict", cfg.Crons.Predict, func(ctx context.Context) error {
			if _, err := client.GenerateFeatures(ctx); err != nil {
				return err
			}
			_, err := client.Predict(ctx, nil)
			return err
		}},
		{"signals", cfg.Crons.Signals, func(ctx context.Context) error {
			_, err := client.GenerateSignals(ctx)
			return err
		}},
		{"evaluate", cfg.Crons.Evaluate, func(ctx context.Context) error {
			_, err := client.Evaluate(ctx)
			return err
		}},
		{"train", cfg.Crons.Train, func(ctx context.Context) error {
			_, err := client.Train(ctx, nil)
			return err
		}},
		{"alerts", cfg.Crons.Alerts, func(ctx context.Context) error {
			// Go-side job: alert evaluation + prediction/price freshness.
			if err := alertRunner.UpdateFreshness(ctx, metrics); err != nil {
				log.Warn("freshness_update_failed", slog.String("error", err.Error()))
			}
			n, err := alertRunner.Run(ctx)
			if err != nil {
				return err
			}
			if n > 0 {
				log.Info("alerts_triggered", slog.Int("events", n))
			}
			return nil
		}},
		{"cleanup", cfg.Crons.Cleanup, func(ctx context.Context) error {
			_, err := client.Cleanup(ctx)
			return err
		}},
	}

	for _, j := range jobs {
		name, fn := j.name, j.fn
		if _, err := s.cron.AddFunc(j.spec, func() { s.runWithLock(name, fn) }); err != nil {
			return nil, err
		}
	}
	return s, nil
}

// Start launches the cron loop.
func (s *Scheduler) Start() {
	s.log.Info("scheduler_started")
	s.cron.Start()
}

// Stop halts scheduling and returns a context that completes when running
// jobs have finished (used during graceful shutdown).
func (s *Scheduler) Stop() context.Context {
	s.log.Info("scheduler_stopping")
	return s.cron.Stop()
}

// runWithLock acquires lock:job:<name> and runs the job, updating metrics.
func (s *Scheduler) runWithLock(name string, fn func(ctx context.Context) error) {
	ctx, cancel := context.WithTimeout(context.Background(), LockTTL)
	defer cancel()

	key := "lock:job:" + name
	ok, err := s.redis.SetNX(ctx, key, s.instanceID, LockTTL).Result()
	if err != nil {
		s.log.Error("job_lock_error", slog.String("job", name), slog.String("error", err.Error()))
		return
	}
	if !ok {
		s.log.Info("job_lock_held_elsewhere", slog.String("job", name))
		return
	}
	defer func() {
		relCtx, relCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer relCancel()
		if err := s.redis.Eval(relCtx, releaseScript, []string{key}, s.instanceID).Err(); err != nil {
			s.log.Warn("job_lock_release_failed", slog.String("job", name), slog.String("error", err.Error()))
		}
	}()

	start := time.Now()
	s.log.Info("job_started", slog.String("job", name))
	err = fn(ctx)
	dur := time.Since(start)
	s.metrics.JobDuration.WithLabelValues(name).Observe(dur.Seconds())
	if err != nil {
		s.metrics.JobFailures.WithLabelValues(name).Inc()
		s.log.Error("job_failed", slog.String("job", name),
			slog.Float64("duration_s", dur.Seconds()), slog.String("error", err.Error()))
		return
	}
	s.metrics.JobLastSuccess.WithLabelValues(name).SetToCurrentTime()
	s.log.Info("job_succeeded", slog.String("job", name), slog.Float64("duration_s", dur.Seconds()))
}
