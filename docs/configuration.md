# Configuration Reference

All configuration is via environment variables (`.env` locally, Docker secrets in production). `.env.example` is the authoritative template; this page explains each setting.

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` / `POSTGRES_PORT` | `postgres` / `5432` | Database address (service name inside compose) |
| `POSTGRES_DB` / `POSTGRES_USER` | `goldpred` | Database name/user |
| `POSTGRES_PASSWORD` | — (required) | DB password. Prod: `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password` |
| `REDIS_ADDR` | `redis:6379` | Redis address (locks, coordination) |
| `INTERNAL_API_TOKEN` | — (required) | Shared secret between Go api and Python service. `_FILE` variant supported |
| `API_PORT` | `8080` | Go API listen port (internal) |
| `JWT_SECRET` | — (required, ≥32 chars) | JWT signing key. `_FILE` variant supported |
| `JWT_TTL_HOURS` | `24` | Token lifetime |
| `ALLOW_OPEN_REGISTRATION` | `false` | When false, only the first user can self-register; the admin creates the rest |
| `PREDICTION_SERVICE_URL` | `http://prediction-service:8500` | Internal URL of the Python service |
| `SCHEDULER_ENABLED` | `true` | Run the cron scheduler in this api instance |
| `RATE_LIMIT_RPM` | `60` | Per-IP request budget per minute (login is separately capped at 10/min) |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:8088` | Comma-separated allowed origins |
| `LOG_LEVEL` | `info` | `debug`/`info`/`warn`/`error` (JSON logs on stdout) |
| `SCHEDULE_COLLECT_CRON` | `*/10 * * * *` | Price collection cadence (UTC) |
| `SCHEDULE_PREDICT_CRON` | `5 * * * *` | Features + prediction generation |
| `SCHEDULE_SIGNALS_CRON` | `10 * * * *` | Signal generation |
| `SCHEDULE_EVALUATE_CRON` | `20 * * * *` | Backfill actuals / live accuracy |
| `SCHEDULE_TRAIN_CRON` | `30 2 * * *` | Model retraining |
| `SCHEDULE_ALERTS_CRON` | `*/5 * * * *` | User alert evaluation |
| `SCHEDULE_CLEANUP_CRON` | `0 4 * * *` | Retention cleanup |
| `PREDICTION_PORT` | `8500` | Python service port (internal) |
| `MODELS_DIR` | `/app/models` | Model artifact volume mount |
| `HTTP_TIMEOUT_SECONDS` | `15` | Outbound provider request timeout |
| `RAW_RETENTION_DAYS` | `365` | Retention for `raw_observations` |
| `STALE_MINUTES` | `30` | Age after which a price is flagged stale (UI banner, signal gate) |
| `NAVASAN_API_KEY` | empty | Optional keyed Iranian FX/gold API; provider disabled when empty |
| `METALS_DEV_API_KEY` | empty | Optional keyed global metals API; disabled when empty |
| `FRONTEND_PORT` | `8088` | Host port publishing the dashboard |

Notes:
- Every secret supports a `*_FILE` variant pointing at a mounted file; when both are set, the file wins.
- Cron expressions are standard 5-field, evaluated in UTC.
- Provider enable/priority is runtime data in the `data_providers` table, not env.
