// Command api is the public Go API server: config, migrations, router,
// scheduler and graceful shutdown.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/admin"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/alerts"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/auth"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/config"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/internalclient"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/issues"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/models"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/portfolio"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/predictions"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/prices"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/scheduler"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/signalsvc"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/storage"
)

const shutdownDrain = 15 * time.Second

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.FromOSEnv()
	if err != nil {
		return err
	}

	logger := newLogger(cfg.LogLevel)
	slog.SetDefault(logger)
	logger.Info("starting", slog.String("service", "backend-go"), slog.String("port", cfg.APIPort))

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// Database.
	pool, err := storage.NewPool(ctx, cfg.DSN(), logger)
	if err != nil {
		return err
	}
	defer pool.Close()

	// Migrations.
	if err := storage.RunMigrations(cfg.DSN(), cfg.MigrationsDir, logger); err != nil {
		return err
	}

	// Issue capture: mirror every WARN/ERROR into app_issues (Issues tab).
	// Wired after migrations so the table exists before the first insert.
	issueRecorder := issues.NewRecorder(pool)
	defer issueRecorder.Close()
	logger = slog.New(issues.NewTeeHandler(logger.Handler(), issueRecorder))
	slog.SetDefault(logger)

	// Redis.
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() { _ = rdb.Close() }()
	if err := rdb.Ping(ctx).Err(); err != nil {
		logger.Warn("redis_unreachable_at_boot", slog.String("error", err.Error()))
	}

	// Shared services.
	metrics := obs.NewMetrics()
	auditLog := audit.New(pool, logger)
	tokens := auth.NewTokenManager(cfg.JWTSecret, time.Duration(cfg.JWTTTLHours)*time.Hour)
	pyClient := internalclient.New(cfg.PredictionServiceURL, cfg.InternalAPIToken, logger)
	alertRunner := &alerts.Runner{
		Pool: pool, Log: logger, StaleMinutesDefault: cfg.StaleMinutes,
		MarketOpen: cfg.MarketTehranOpen, MarketClose: cfg.MarketTehranClose,
	}

	// Rate limiters (stopped on shutdown).
	globalLimiter := httpserver.NewRateLimiter(cfg.RateLimitRPM)
	defer globalLimiter.Stop()
	loginLimiter := httpserver.NewRateLimiter(10)
	defer loginLimiter.Stop()

	router := httpserver.NewRouter(cfg, httpserver.Deps{
		Logger:  logger,
		Metrics: metrics,
		Verify:  auth.VerifyForMiddleware(tokens),
		Health:  obs.HealthHandler(),
		Readiness: obs.ReadinessHandler(metrics,
			func(ctx context.Context) error { return pool.Ping(ctx) },
			func(ctx context.Context) error { return rdb.Ping(ctx).Err() },
		),
		Auth: &auth.Handler{
			Pool: pool, Tokens: tokens, Audit: auditLog, Log: logger,
			AllowOpenRegistration: cfg.AllowOpenRegistration,
		},
		Prices: &prices.Handler{
			Pool: pool, Log: logger, StaleMinutesDefault: cfg.StaleMinutes,
			MarketOpen: cfg.MarketTehranOpen, MarketClose: cfg.MarketTehranClose,
		},
		Predictions: &predictions.Handler{Pool: pool, Log: logger, Client: pyClient},
		Signals:     &signalsvc.Handler{Pool: pool, Log: logger},
		Models:      &models.Handler{Pool: pool, Log: logger},
		Portfolio:   &portfolio.Handler{Pool: pool, Audit: auditLog, Log: logger},
		Alerts:      &alerts.Handler{Pool: pool, Audit: auditLog, Log: logger},
		Admin:       &admin.Handler{Pool: pool, Client: pyClient, Audit: auditLog, Log: logger},
		Issues:      &issues.Handler{Pool: pool, Recorder: issueRecorder, Log: logger},

		GlobalLimiter: globalLimiter,
		LoginLimiter:  loginLimiter,
	})

	// Scheduler.
	var sched *scheduler.Scheduler
	if cfg.SchedulerEnabled {
		sched, err = scheduler.New(cfg, rdb, pyClient, alertRunner, metrics, logger)
		if err != nil {
			return fmt.Errorf("scheduler: %w", err)
		}
		sched.Start()
	} else {
		logger.Info("scheduler_disabled")
	}

	server := &http.Server{
		Addr:              ":" + cfg.APIPort,
		Handler:           router,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		logger.Info("http_listening", slog.String("addr", server.Addr))
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	select {
	case err := <-serverErr:
		return err
	case <-ctx.Done():
	}

	// Graceful shutdown: stop cron, then drain HTTP for up to 15s.
	logger.Info("shutdown_started")
	if sched != nil {
		cronCtx := sched.Stop()
		select {
		case <-cronCtx.Done():
		case <-time.After(shutdownDrain):
			logger.Warn("cron_jobs_still_running_at_deadline")
		}
	}
	drainCtx, cancel := context.WithTimeout(context.Background(), shutdownDrain)
	defer cancel()
	if err := server.Shutdown(drainCtx); err != nil {
		logger.Error("http_shutdown_error", slog.String("error", err.Error()))
	}
	logger.Info("shutdown_complete")
	return nil
}

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	switch level {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl}))
}
