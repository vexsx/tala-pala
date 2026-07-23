# Troubleshooting Guide

Run `make ps` first — every service has a healthcheck, and most problems show up there.

## Stack won't start
- `docker compose logs api --tail=50` — the api fails fast with a clear message when required env vars are missing (e.g. `POSTGRES_PASSWORD`, `JWT_SECRET` too short).
- `.env` missing → `cp .env.example .env` and set secrets, or run `scripts/init.sh`.
- Port conflict on 8088 → change `FRONTEND_PORT` in `.env`.

## "api unhealthy" / migration errors
- `docker compose logs api | grep -i migrat` — a dirty migration state after a crash can be inspected with `docker compose exec postgres psql -U goldpred -c 'select * from schema_migrations;'`.
- **Recovery**: `make migrate-force VERSION=n` where *n* is the last known-GOOD version (if `schema_migrations` shows `version=15, dirty=t`, migration 15 failed midway — inspect/undo its partial effects, then `make migrate-force VERSION=14`). Then `make migrate` (restart api) re-applies forward. The dirty flag exists because a half-applied migration needs a human decision; force only after checking what was actually applied.

## No prices / stale banner in the UI
1. `docker compose logs prediction-service --tail=100 | grep -i collect`
2. Check provider health: Overview page, or `GET /api/v1/market/summary`.
3. Trigger manually: `make collect`. Individual provider failures are normal — the registry falls back by priority. If **all** Iranian providers fail, the ⚠ stale banner appears and signals force `hold`; this is by design, never silently served.
4. Providers can be disabled/re-prioritized in the `data_providers` table.

## No predictions / "horizon disabled"
- Horizons need history: ≥120 daily points for daily horizons, ≥14 days of hourly data for 1h/4h. Seed history first: `docker compose exec prediction-service python -m app.seed.seed_history`, then `make train predict`.

## Training fails or model never activates
- `docker compose logs prediction-service | grep -i train`. A model that doesn't beat the naive baseline is *not* activated — naive being served is honest behavior, not a bug.
- Check `training_runs` table for the error column.

## Login problems
- Rate limiter returns 429 after 10 login attempts/min/IP — wait a minute.
- Password reset (no self-service flow): create a temp user with the desired password, copy its hash over, then delete it:
  ```bash
  docker compose exec api /app/createuser -email temp@local -password 'NewPass123!' -role user
  docker compose exec postgres psql -U goldpred -d goldpred -c \
    "UPDATE users SET password_hash=(SELECT password_hash FROM users WHERE email='temp@local') WHERE email='you@example.com'; DELETE FROM users WHERE email='temp@local';"
  ```

## Redis down
- Scheduler jobs skip runs while the lock store is unavailable (logged); the API keeps serving. `docker compose restart redis`.

## Disk filling up
- `raw_observations` retention defaults to 365 days (`RAW_RETENTION_DAYS`); cleanup runs daily.
- Docker log rotation: see docs/deployment.md.

## Frontend image fails to build
- The frontend compiles inside Docker (`node:20-alpine`) with `vite build`, which catches syntax and import errors but does not type-check. Full strict type-checking is available separately with `npm run typecheck` (requires Node locally or in CI). If `docker compose build frontend` fails, the error names the exact file/line.

## Frontend loads but API calls fail (502/504)
- The frontend nginx proxies `/api/` to `api:8080`; check `docker compose logs frontend api`. After changing CORS or ports, rebuild: `make up`.

## Model degradation alert fired
- Check Models page: live accuracy vs baseline. Retrain (`make train`); if degradation persists, the market regime likely shifted — the naive baseline will be served until a challenger beats it again.
