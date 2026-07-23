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

// LockTTL is the default Redis lock expiry (and job context timeout). Jobs
// that legitimately run longer declare their own timeout below; the lock TTL
// always matches the job timeout so a second replica cannot start the same
// job while a long run is still in flight.
const LockTTL = 10 * time.Minute

// trainTimeout must exceed a full training run (30–36 min observed in
// production for both symbols). The old 10-minute default cancelled the HTTP
// call mid-run: Go recorded job_failed while Python kept training.
const trainTimeout = internalclient.TrainTimeout

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
		name    string
		spec    string
		timeout time.Duration
		fn      func(ctx context.Context) error
	}{
		{"collect", cfg.Crons.Collect, LockTTL, func(ctx context.Context) error {
			_, err := client.Collect(ctx, nil)
			return err
		}},
		{"predict", cfg.Crons.Predict, LockTTL, func(ctx context.Context) error {
			if _, err := client.GenerateFeatures(ctx); err != nil {
				return err
			}
			_, err := client.Predict(ctx, nil)
			return err
		}},
		{"signals", cfg.Crons.Signals, LockTTL, func(ctx context.Context) error {
			_, err := client.GenerateSignals(ctx)
			return err
		}},
		{"evaluate", cfg.Crons.Evaluate, LockTTL, func(ctx context.Context) error {
			_, err := client.Evaluate(ctx)
			return err
		}},
		{"train", cfg.Crons.Train, trainTimeout, func(ctx context.Context) error {
			_, err := client.Train(ctx, nil)
			return err
		}},
		{"alerts", cfg.Crons.Alerts, LockTTL, func(ctx context.Context) error {
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
		{"cleanup", cfg.Crons.Cleanup, LockTTL, func(ctx context.Context) error {
			_, err := client.Cleanup(ctx)
			return err
		}},
	}

	for _, j := range jobs {
		name, timeout, fn := j.name, j.timeout, j.fn
		if _, err := s.cron.AddFunc(j.spec, func() { s.runWithLock(name, timeout, fn) }); err != nil {
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
// The lock TTL equals the job timeout so the lock outlives the whole run.
func (s *Scheduler) runWithLock(name string, timeout time.Duration, fn func(ctx context.Context) error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	key := "lock:job:" + name
	ok, err := s.redis.SetNX(ctx, key, s.instanceID, timeout).Result()
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
