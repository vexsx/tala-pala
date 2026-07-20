// Package storage owns database connectivity, migrations and small shared
// query helpers. Every query in this codebase is parameterized.
package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/golang-migrate/migrate/v4"
	migratepgx "github.com/golang-migrate/migrate/v4/database/pgx/v5"
	_ "github.com/golang-migrate/migrate/v4/source/file"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jackc/pgx/v5/stdlib"
)

// NewPool creates a pgx connection pool and verifies connectivity, retrying
// for up to ~30 seconds so the API survives a slow database boot.
func NewPool(ctx context.Context, dsn string, log *slog.Logger) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MaxConnLifetime = time.Hour

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("create pool: %w", err)
	}

	deadline := time.Now().Add(30 * time.Second)
	for {
		pingCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
		err = pool.Ping(pingCtx)
		cancel()
		if err == nil {
			return pool, nil
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			pool.Close()
			return nil, fmt.Errorf("database unreachable: %w", err)
		}
		log.Warn("db_ping_retry", slog.String("error", err.Error()))
		select {
		case <-ctx.Done():
			pool.Close()
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

// RunMigrations applies all up migrations from dir (file:// source).
func RunMigrations(dsn, dir string, log *slog.Logger) error {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return fmt.Errorf("open sql db: %w", err)
	}
	defer func() { _ = db.Close() }()

	driver, err := migratepgx.WithInstance(db, &migratepgx.Config{})
	if err != nil {
		return fmt.Errorf("migrate driver: %w", err)
	}
	m, err := migrate.NewWithDatabaseInstance("file://"+dir, "pgx5", driver)
	if err != nil {
		return fmt.Errorf("migrate init (dir=%s): %w", dir, err)
	}
	err = m.Up()
	if errors.Is(err, migrate.ErrNoChange) {
		log.Info("migrations_up_to_date")
		return nil
	}
	if err != nil {
		return fmt.Errorf("migrate up: %w", err)
	}
	version, dirty, _ := m.Version()
	log.Info("migrations_applied", slog.Uint64("version", uint64(version)), slog.Bool("dirty", dirty))
	return nil
}

// GetStaleMinutes reads the stale-price threshold from app_settings, falling
// back to the provided default when unset or unreadable.
func GetStaleMinutes(ctx context.Context, pool *pgxpool.Pool, fallback int) int {
	var minutes int
	err := pool.QueryRow(ctx,
		`SELECT (value)::int FROM app_settings WHERE key = 'stale_price_threshold_minutes'`).
		Scan(&minutes)
	if err != nil || minutes <= 0 {
		return fallback
	}
	return minutes
}

// Ensure the pgx stdlib driver is linked (registers "pgx" with database/sql).
var _ = stdlib.GetDefaultDriver
