# prediction-python

Internal-only FastAPI service (port 8500) that collects market data, engineers
point-in-time features, trains/compares forecast models with walk-forward
validation, writes predictions and buy/hold/sell signals, and runs cost-aware
backtests for the Iran gold analysis stack.

The Postgres schema is owned by the Go service's migrations
(`database/migrations/`); this service only mirrors it as SQLAlchemy metadata
(`app/db.py`) and never creates or alters tables in production.

## Endpoints

All under `/internal/*` and guarded by the `X-Internal-Token` header except
`/internal/health` and `/internal/metrics`. See `docs/CONTRACTS.md` for the
full contract (collect, features/generate, train, predict, signals/generate,
backtest, evaluate, maintenance/cleanup, providers/health).

## Data providers

Registered in `data_providers` (migrations 0001/0004/0007) and mapped to
classes in `app/providers/registry.py`: `tgju` (primary Iranian source, rial
÷10 → toman), `alanchand` (two modes: documented Bearer-token API with
`ALANCHAND_TOKEN`, otherwise keyless HTML parsing of the public
`/en/gold-price/18ayar` page — 18k only, rial ÷10), `milligold` (keyless HTML
parse of milli.gold's server-rendered 18k gram price, rial ÷10, handles
Persian digits), `pricedb` (keyless mirror of TGJU quotes from the
MIT-licensed github.com/margani/pricedb dataset, rial ÷10), `gold_api`
(keyless api.gold-api.com XAU/XAG spot), `yahoo`, `stooq`, plus optional keyed
providers `navasan` (`NAVASAN_API_KEY`), `metals_dev` (`METALS_DEV_API_KEY`)
and `brsapi` (`BRSAPI_KEY`; BrsApi.ir quotes Iranian items in **toman**, so no
÷10 — the global ounce is USD). HTML modes mark `raw_unit` as
`IRR/gram (html)` so the source method stays auditable, and fail gracefully
(never bypass) if a page starts serving challenges.

## Market-hours awareness (Addendum 1)

`app/core/market_hours.py`: Iranian symbols trade Sat–Thu between
`MARKET_TEHRAN_OPEN`/`MARKET_TEHRAN_CLOSE` (Asia/Tehran; open inclusive,
close exclusive; all Friday closed); global symbols are closed Fri 21:00 UTC
→ Sun 22:00 UTC. While a market is OPEN staleness is the plain
`STALE_MINUTES` age rule; while CLOSED, data from the last session (observed
no earlier than closure start − `STALE_MINUTES`) still counts as fresh. The
collect gate, prediction warnings and signal engine all use
`is_acceptably_fresh`: last-session data during a closure no longer forces
`hold` — the signal/prediction just carries a "prices from last session
(market closed)" note; truly stale data still forces hold.

## Live calibration

`POST /internal/evaluate` also refreshes per-horizon rolling stats over the
last up-to-60 matured predictions (directional hit rate + coverage of the
nominal 90% interval), persisted to `app_settings['live_calibration']`. The
prediction pass blends confidence toward the live hit rate
(`w = max(0.3, 1 - n/60)`, clamped to [0.05, 0.95]), widens intervals that
recently under-covered (< 0.75 with n ≥ 20; factor `0.9/coverage` capped at
1.5×) with a warning, and re-weights an active ensemble by inverse *live*
sMAPE once every member has ≥ 20 matured predictions.

## Model families

`naive`, `sma`, `ses` (SES/Holt), `arima` (small AIC grid), `theta`
(statsmodels ThetaModel with a two-theta-line fallback), `holt_damped`
(damped-trend Holt), `sarimax_exog` (SARIMAX on log-returns with point-in-time
exogenous USD_IRT/XAU log-returns + premium z-score; future exog held at the
last known values, skipped when the exog series are unavailable), `linear`
(Ridge), `rf`, `gbr`, `quantile_gbr` (three quantile GBRs at 5/50/95% — the
median is the point forecast and the outer quantiles provide the model's own
native interval, used by the prediction pass instead of residual quantiles),
`hist_gb` (HistGradientBoosting), `knn_analogue` (pure-numpy pattern
analogues: z-scored last-20-returns window matched to its 25 nearest
historical windows), and an inverse-sMAPE-weighted `ensemble` of everything
that beats naive. A candidate is only activated when it beats the naive
baseline's sMAPE on the same walk-forward folds. Heavy DL frameworks
(torch/tensorflow/prophet/xgboost) are deliberately excluded — at this data
scale (a few hundred daily points) sklearn ensembles + classical time-series
models are sufficient and far cheaper to run.

## Features (Addendum 2)

`app/features/engineering.py` adds point-in-time-safe indicator features:
ADX(14), Stochastic %K/%D (14,3), Williams %R(14), CCI(20), distance from
the Donchian(20) upper/lower bands, position vs the Keltner(20, 2×ATR) mid,
rolling 20d correlation of 18k vs XAUUSD log-returns, drawdown % from the
90d high, 5d premium momentum, and day-of-week one-hots. The daily series is
synthesized from ticks (no true OHLC), so intraday high/low are approximated
with backward rolling max/min of the daily closes — a documented
approximation; everything passes the leakage guard tests.

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m pytest
```

Run the API locally (needs `DATABASE_URL` or `POSTGRES_*` and
`INTERNAL_API_TOKEN` in the environment):

```bash
.venv/Scripts/python -m uvicorn --factory app.main:get_app --port 8500
```

Backfill history (TGJU daily history, then the pricedb GitHub dataset, then
Yahoo, bundled sample CSVs as a last resort; idempotent):

```bash
python -m app.seed.seed_history            # network + samples
python -m app.seed.seed_history --offline  # bundled samples only
python -m app.seed.seed_history --from-csv path.csv --symbol IR_GOLD_18K
```

Sample CSVs in `data_samples/` are deterministic fixtures generated by
`scripts/make_samples.py` (seeded random walk; not real market data).

## Configuration

Per the repository `.env.example`: `DATABASE_URL` (or `POSTGRES_*`),
`INTERNAL_API_TOKEN`, `PREDICTION_PORT`, `MODELS_DIR`,
`HTTP_TIMEOUT_SECONDS`, `RAW_RETENTION_DAYS`, `STALE_MINUTES`,
`MARKET_TEHRAN_OPEN` / `MARKET_TEHRAN_CLOSE` (default 12:00/20:00), optional
`NAVASAN_API_KEY` / `METALS_DEV_API_KEY` / `BRSAPI_KEY` /
`ALANCHAND_TOKEN`. `POSTGRES_PASSWORD_FILE` and
`INTERNAL_API_TOKEN_FILE` (Docker secrets) take precedence over the plain
variables.
