# Service Contracts (source of truth)

All services MUST conform to this document. Database DDL lives in `database/migrations/` and is authoritative for storage.

## Units and symbols (critical)

- Internal canonical currency for Iranian values: **IRT (toman)**. 1 toman = 10 rials. TGJU quotes in **rials**; providers must divide by 10 during normalization and record the raw rial value in `raw_observations`.
- All timestamps stored/exchanged in **UTC ISO-8601** (`2026-07-20T10:00:00Z`). Frontend renders Asia/Tehran + Jalali.
- Canonical symbols in `prices.symbol`:

| symbol | meaning | currency | unit |
|---|---|---|---|
| `IR_GOLD_18K` | Iranian 18k gold per gram | IRT | gram |
| `XAUUSD` | Global gold | USD | ozt |
| `XAGUSD` | Silver | USD | ozt |
| `USD_IRT` | Free-market USD rate | IRT | usd |
| `IR_COIN_EMAMI` | Emami coin | IRT | coin |
| `BRENT_OIL` | Brent | USD | bbl |
| `DXY` | Dollar index | INDEX | index |
| `US10Y` | US 10-year yield | PCT | pct |

- Theoretical formula (implemented in Python `core/formula.py`, validated by tests):
  - `pure_gram_usd = xau_usd / 31.1034768`
  - `pure_gram_irt = pure_gram_usd * usd_irt`
  - `k18_irt = pure_gram_irt * 0.750`
  - `premium_pct = (observed_18k - k18_irt) / k18_irt * 100`

- Horizons: `1h`, `4h`, `eod`, `1d`, `3d`, `7d`, `30d`. A horizon is only "enabled" when data coverage supports it; Python decides and records warnings.

## Python prediction-service (internal-only, port 8500)

Auth: every `/internal/*` request requires header `X-Internal-Token: $INTERNAL_API_TOKEN`. 401 otherwise. Never exposed publicly (Docker internal network only).

- `GET /internal/health` → `{"status":"ok","db":true,"version":"..."}` (no token required)
- `GET /internal/providers/health` → `[{"code","name","category","enabled","priority","healthy","last_success_at","consecutive_failures","last_error"}]`
- `POST /internal/collect` body `{"jobs":["iran_gold","fx","global","macro"]}` (empty=all) → `{"collected":{"IR_GOLD_18K":1,...},"errors":[...]}` — fetch from providers (priority order, fallback), validate, dedupe, write `raw_observations`+`prices`, update `data_providers` health.
- `POST /internal/features/generate` → builds `feature_snapshots` for `IR_GOLD_18K` from `prices` (point-in-time correct).
- `POST /internal/train` body `{"horizons":["1d",...]}` (empty=all enabled) → walk-forward model comparison per horizon, writes `training_runs`, `model_versions` (activates winner only if it beats naive baseline), saves artifacts under `/app/models`.
- `POST /internal/predict` body `{"horizons":[...]}` → uses active model per horizon, writes rows to `predictions`, returns them.
- `POST /internal/signals/generate` → composes latest predictions + indicators + premium into one row in `signals`, returns it.
- `POST /internal/backtest` body `{"horizon":"1d","fee_pct":0.5,"spread_pct":1.0,"slippage_pct":0.1,"min_holding_days":1,"start":null,"end":null}` → writes `backtest_runs`, returns results JSON (strategy vs buy_and_hold vs sma_crossover vs no_action; metrics: total_return_pct, annualized_return_pct, win_rate, profit_factor, max_drawdown_pct, n_trades, avg_trade_return_pct, sharpe_like, directional_accuracy, per-regime table, gross vs net).
- `POST /internal/evaluate` → fills `predictions.actual_value` for matured predictions; returns live-accuracy summary.
- `POST /internal/maintenance/cleanup` → prune old raw_observations per retention config.
- `GET /internal/metrics` → Prometheus text format (no token; scraped internally). Metric names: `goldpred_collect_success_total{provider,symbol}`, `goldpred_collect_failure_total{provider}`, `goldpred_last_price_timestamp_seconds{symbol}`, `goldpred_prediction_duration_seconds`, `goldpred_model_smape{horizon,model}`, `goldpred_job_last_success_timestamp_seconds{job}`.

Python reads/writes Postgres directly (SQLAlchemy). Nothing else calls Python except the Go scheduler/proxy.

## Go api (public, port 8080)

Sends `X-Internal-Token` when calling Python. Reads Postgres for all GET endpoints (does NOT call Python on the request path, except admin trigger endpoints which proxy to Python).

Response envelope: success → raw JSON payload; errors → `{"error":{"code":"string","message":"string","details":{}}}` with proper HTTP status. Every response includes `X-Request-ID`.

Auth: `POST /api/v1/auth/register {email,password}` (min 10 chars password; first registered user becomes `admin`, later registrations require admin unless `ALLOW_OPEN_REGISTRATION=true`), `POST /api/v1/auth/login {email,password}` → `{"token","expires_at","user":{"id","email","role"}}` (JWT HS256, `JWT_SECRET`, TTL `JWT_TTL_HOURS`), `GET /api/v1/auth/me`. Protected endpoints use `Authorization: Bearer <token>`.

Public (no auth): `/api/v1/health`, `/api/v1/readiness`, `/metrics` (Prometheus).

Authenticated endpoints:
- `GET /api/v1/prices/current` → `{"prices":{"IR_GOLD_18K":{"value","currency","unit","source","observed_at","stale":bool,"change_24h_pct"},...},"as_of":"..."}`
- `GET /api/v1/prices/history?symbol=IR_GOLD_18K&from=...&to=...&interval=raw|hourly|daily&page=1&page_size=500` → `{"items":[{"observed_at","value","source"}],"page","page_size","total"}`
- `GET /api/v1/market/summary` → current 18k, change_24h_pct, xau, usd_irt, theoretical_18k, premium_pct, premium_avg_30d, last_update, providers:[health], latest signal summary
- `GET /api/v1/market/premium?days=90` → history of theoretical vs observed + premium series
- `GET /api/v1/market/indicators?days=90` → computed in Go from prices: sma_20, sma_50, ema_12, ema_26, rsi_14, macd{line,signal,hist}, bollinger{upper,mid,lower}, atr_14, momentum_10, roc_10, volatility_20, support, resistance (daily series)
- `GET /api/v1/predictions` → latest prediction per horizon
- `GET /api/v1/predictions/{horizon}?limit=50` → history incl. actual_value
- `GET /api/v1/signals/current` → latest signals row
- `GET /api/v1/signals/history?limit=50`
- `GET /api/v1/models` → model_versions (active + recent)
- `GET /api/v1/models/performance` → per-horizon active model metrics vs baseline + live accuracy (from matured predictions) + last training run
- Portfolio (scoped to authed user): `GET /api/v1/portfolio` → holdings + computed {total_grams_18k_equivalent, invested, current_value, unrealized_pnl, pnl_pct, avg_price, break_even_price, scenarios:[{change_pct,value,pnl}], target_price_for_profit_pct(10)}, `POST /api/v1/portfolio/transactions`, `PUT/DELETE /api/v1/portfolio/transactions/{id}`, `POST /api/v1/portfolio/import` (multipart CSV, max 1MB, columns: tx_type,grams,karat,price_per_gram,currency,fees,tx_date,notes), `GET /api/v1/portfolio/export` (CSV; cells starting with =+-@ prefixed with ' to block formula injection)
- Alerts: `GET/POST /api/v1/alerts`, `PUT/DELETE /api/v1/alerts/{id}`, `GET /api/v1/alerts/events?unacked=true`, `POST /api/v1/alerts/events/{id}/ack`
- Admin only: `POST /api/v1/admin/jobs/{collect|train|predict|signals|backtest|evaluate}` (proxy to Python), `GET /api/v1/admin/audit?page=`

Karat conversion for portfolio: value of k-karat grams priced via 18k price × (k/18) — documented approximation.

Scheduler (in Go api, `SCHEDULER_ENABLED=true`): cron jobs acquire Redis lock `lock:job:<name>` via SET NX PX before running; call Python endpoints. Default intervals (env-overridable): collect `*/10 * * * *`, features+predict hourly, signals hourly (after predict), evaluate hourly, train daily 02:30 UTC, provider health in collect, alert evaluation every 5m (Go-side, reads DB, writes alert_events), cleanup daily.

Go alert evaluation handles: price_above/below, signal_change, confidence_above, volatility_spike, premium_above, stale_data, provider_failure, model_degradation.

Rate limiting: token bucket per IP (default 60 req/min, env `RATE_LIMIT_RPM`), login stricter (10/min). Security headers + CORS from `CORS_ALLOWED_ORIGINS`.

## Frontend (Vite React, served by nginx on port 80 in-container)

Calls the API at same-origin `/api/v1/...` (nginx in the frontend container proxies `/api/` and `/metrics` is NOT exposed). Login page stores JWT in memory + localStorage. Pages: Overview, Forecast, Technical, Drivers, Portfolio, Alerts, Models. Global banner: "Predictions are uncertain estimates, not financial advice." Numbers displayed in toman with thousands separators; toggle IRT/IRR display (display-only ×10); Jalali+Gregorian date toggle.

## Environment variables (.env.example is authoritative)

Shared: `POSTGRES_HOST/PORT/DB/USER/PASSWORD`, `REDIS_ADDR`, `INTERNAL_API_TOKEN`.
Go: `API_PORT=8080`, `JWT_SECRET`, `JWT_TTL_HOURS=24`, `ALLOW_OPEN_REGISTRATION=false`, `PREDICTION_SERVICE_URL=http://prediction-service:8500`, `SCHEDULER_ENABLED=true`, `RATE_LIMIT_RPM=60`, `CORS_ALLOWED_ORIGINS`, `LOG_LEVEL=info`, cron overrides `SCHEDULE_COLLECT_CRON` etc.
Python: `PREDICTION_PORT=8500`, `DATABASE_URL=postgresql+psycopg://...`, `MODELS_DIR=/app/models`, `HTTP_TIMEOUT_SECONDS=15`, `NAVASAN_API_KEY=` (optional), `METALS_DEV_API_KEY=` (optional), `RAW_RETENTION_DAYS=365`, `STALE_MINUTES=30`.
Both support `*_FILE` variants for Docker secrets (e.g. `POSTGRES_PASSWORD_FILE`).
