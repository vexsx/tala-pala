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
- `POST /internal/predict/custom` body `{"days":N, "fee_pct"?, "spread_pct"?, "slippage_pct"?}` (1 ≤ N ≤ 90) → on-demand forecast at exactly N daily steps: walk-forward validates a fast candidate subset, returns point/interval/direction/confidence plus a cost-aware `decision_lean` (buy/hold/sell). Ephemeral — nothing persisted, no artifact written. 400 on bad input or insufficient history.
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
- Admin user management (Addendum 5; self-registration is closed by default): `GET /api/v1/admin/users` (list + portfolio tx counts), `POST /api/v1/admin/users {email,password,role}`, `PUT /api/v1/admin/users/{id} {role?,password?}`, `DELETE /api/v1/admin/users/{id}`. Guards: an admin cannot delete their own account; the last admin can neither be deleted nor demoted (409). All actions audited.

Karat conversion for portfolio: value of k-karat grams priced via 18k price × (k/18) — documented approximation.

Scheduler (in Go api, `SCHEDULER_ENABLED=true`): cron jobs acquire Redis lock `lock:job:<name>` via SET NX PX before running; call Python endpoints. Default intervals (env-overridable): collect `*/10 * * * *`, features+predict hourly, signals hourly (after predict), evaluate hourly, train daily 02:30 UTC, provider health in collect, alert evaluation every 5m (Go-side, reads DB, writes alert_events), cleanup daily.

Go alert evaluation handles: price_above/below, signal_change, confidence_above, volatility_spike, premium_above, stale_data, provider_failure, model_degradation.

Rate limiting: token bucket per IP (default 60 req/min, env `RATE_LIMIT_RPM`), login stricter (10/min). Security headers + CORS from `CORS_ALLOWED_ORIGINS`.

## Frontend (Vite React, served by nginx on port 80 in-container)

Calls the API at same-origin `/api/v1/...` (nginx in the frontend container proxies `/api/` and `/metrics` is NOT exposed). Login page stores JWT in memory + localStorage. Pages: Overview, Forecast, Technical, Drivers, Portfolio, Alerts, Models. Global banner: "Predictions are uncertain estimates, not financial advice." Numbers displayed in toman with thousands separators; toggle IRT/IRR display (display-only ×10); Jalali+Gregorian date toggle.

## Addendum 1 — market-hours awareness (2026-07-20)

Iranian off-days are **Thursday and Friday** (Asia/Tehran days) — revised 2026-07-21. **Revised again 2026-07-23 (Addendum 11): IR_GOLD_18K and USD_IRT are ALWAYS open** — their primary sources quote 24/7 every day of the week (Hamrah Gold; the USDT market). Only the plain STALE_MINUTES age rule applies to them; they never enter a closure. IR_COIN_EMAMI trades Sat–Wed within `MARKET_TEHRAN_OPEN`(default 12:00)–`MARKET_TEHRAN_CLOSE`(default 20:00), open-inclusive/close-exclusive, closed all Thursday and Friday. TSE fund symbols: Sat–Wed 12:00–18:00 Tehran. Global symbols (XAUUSD, XAGUSD, BRENT_OIL, DXY, US10Y) are closed from Fri 21:00 UTC (inclusive) to Sun 22:00 UTC (exclusive). Both Go and Python implement identical rules from the same env vars.

- Every per-symbol price object (`/prices/current` entries, summary `current_18k`/`xau_usd`/`usd_irt`) gains `"market_state": "open"|"closed"`.
- `stale` semantics: while the market is OPEN, stale = older than STALE_MINUTES (unchanged). While CLOSED, data observed within the last session (≤ closed-duration + STALE_MINUTES) is NOT stale; older is.
- Python signal engine uses the same rule: market-closed last-session data does not force `hold`; the signal carries a note "prices from last session (market closed)". The stale_data alert evaluator (Go) also respects this.

## Addendum 2 — expanded indicators (2026-07-20)

`GET /market/indicators` adds scalars: `adx_14`, `stoch_k`, `stoch_d` (14,3), `williams_r_14`, `cci_20`, `donchian` {upper,lower} (20), `keltner` {upper,mid,lower} (20,2×ATR), `corr_xau_20` (rolling correlation of daily log-returns 18k vs XAUUSD), `drawdown_pct` (from 90d high). Series rows gain `adx_14`, `stoch_k`, `stoch_d`. Frontend Technical page displays each with a one-line plain-language meaning.

New model names that may appear in `model_versions.model_name` / predictions: `theta`, `sarimax_exog`, `quantile_gbr`, `hist_gb`, `knn_analogue`, `holt_damped`.

## Environment variables (.env.example is authoritative)

Shared: `POSTGRES_HOST/PORT/DB/USER/PASSWORD`, `REDIS_ADDR`, `INTERNAL_API_TOKEN`.
Go: `API_PORT=8080`, `JWT_SECRET`, `JWT_TTL_HOURS=24`, `ALLOW_OPEN_REGISTRATION=false`, `PREDICTION_SERVICE_URL=http://prediction-service:8500`, `SCHEDULER_ENABLED=true`, `RATE_LIMIT_RPM=60`, `CORS_ALLOWED_ORIGINS`, `LOG_LEVEL=info`, cron overrides `SCHEDULE_COLLECT_CRON` etc.
Python: `PREDICTION_PORT=8500`, `DATABASE_URL=postgresql+psycopg://...`, `MODELS_DIR=/app/models`, `HTTP_TIMEOUT_SECONDS=15`, `NAVASAN_API_KEY=` (optional), `METALS_DEV_API_KEY=` (optional), `RAW_RETENTION_DAYS=365`, `STALE_MINUTES=30`.
Both support `*_FILE` variants for Docker secrets (e.g. `POSTGRES_PASSWORD_FILE`).

## Addendum 3 — issue log, provider gap, custom horizons (2026-07-21)

**Issue log.** Migration `0008_app_issues` adds `app_issues(id, occurred_at, service, level, source, message, details, created_at)` with `service ∈ {api, prediction, frontend}` and `level ∈ {warning, error}`. Both services mirror every WARN/ERROR log record into it (Go: slog tee handler, async + drop-on-saturation; Python: logging handler with re-entrancy guard). The Go API serves `GET /api/v1/issues` and `GET /api/v1/issues/report` (Markdown digest: recent issues + provider health + training runs) — both **admin-only** (the issue log is system scope; regular users get 403) — plus `POST /api/v1/issues` (frontend error reports, service forced to `frontend`, open to any authenticated session so user-side crashes are still captured). Rows older than 30 days are pruned by the Python cleanup job.

**Provider gap.** `GET /api/v1/market/provider-gap?symbol=IR_GOLD_18K&window_minutes=120&history_days=30` reports the dispersion between providers' latest good quotes (per-provider values, `gap_pct = (max-min)/median*100`, daily history). The prediction service computes the same gap before writing predictions: a gap ≥ 1% widens the interval by half the gap on each side and appends a warning. Rationale: cross-provider spread is *quote* uncertainty, orthogonal to model uncertainty.

**Tehran session default.** `MARKET_TEHRAN_OPEN` default changed 09:00 → 12:00 (observed market practice); `.env` on deployments should be updated to match.

**Train timeout.** The Go internal-client timeout for `/internal/train` rose from 120s to 30m — full walk-forward over all candidate families takes minutes on small hosts, and the old budget aborted training mid-run.

## Addendum 4 — self-learning core, trading indicators, candles (2026-07-21)

**Wider ML feature surface.** Tabular models (`linear`, `rf`, `gbr`, `hist_gb`, `quantile_gbr`) now receive the exogenous context (`usd_irt`, `xau_usd`) via `set_context` and train on the full causal feature frame — USD/XAU returns, premium level/z-score/momentum, and the Addendum-2 indicator features. Exog series are truncated at the fold's last gold timestamp (same point-in-time policy as `sarimax_exog`); contexts are stripped from pickled artifacts.

**Adaptive conformal intervals.** Empirical residual intervals now use an ACI-style effective miscoverage level: `alpha_eff = 0.1 + 0.5*(live_coverage − 0.9)`, clamped to [0.02, 0.30], driven by the live coverage stats in `app_settings['live_calibration']` (`models/intervals.adaptive_alpha`). Models with native intervals (quantile_gbr) keep the multiplicative widening.

**Meta-labeling gate** (`models/metagate.py`). The evaluate job refits a logistic model on the system's own matured predictions (features stored at prediction time; label = direction hit) and persists it to `app_settings['meta_gate']`. The prediction pass blends confidence 50/50 with the gate's P(hit), records a `self_assessment` driver, and warns when the gate rates a call below coin-flip. Requires ≥40 matured non-flat predictions.

**Per-regime live calibration.** `app_settings['live_calibration']` entries gain `by_regime: {regime: {n, dir_hit_rate}}`; `blended_confidence` prefers the current regime's hit rate when that regime has ≥10 matured predictions.

**New indicators (Go).** `internal/indicators`: Ichimoku (9/26/52, undisplaced), SuperTrend(10,3) with direction, Parabolic SAR (0.02/0.02/0.2), classic pivot points. `GET /api/v1/market/indicators` gains latest-value fields `ichimoku`, `supertrend`, `psar`, `pivots`; the per-point series now serializes under `items` with **nested** `macd`/`bollinger` objects plus `momentum_10`/`roc_10`/`volatility_20` (matching frontend/src/api/types.ts, which was always the published contract).

**Candles feed.** `GET /api/v1/market/candles?symbol&interval=daily|hourly&days` → true OHLC buckets (first/max/min/last per bucket) + index-aligned overlay arrays (sma 20/50, bollinger, supertrend + dir, psar, four ichimoku lines) + classic pivots from the last completed bar + support/resistance. Feeds the dashboard's Trade panel (lightweight-charts).

## Addendum 6 — TradingView-community-inspired candidates (2026-07-21)

Techniques reimplemented from published descriptions of popular TradingView prediction scripts (no Pine code copied; script licenses vary). All enter the standard walk-forward tournament and activate only when beating naive.

- `lorentzian_knn` — kNN over indicator feature vectors (RSI, stoch %K, momentum, SMA z-score, volatility) with Lorentzian distance `Σ ln(1+|xᵢ−yᵢ|)` and chronologically-spaced neighbor selection (inspired by "Machine Learning: Lorentzian Classification"). In `CANDIDATES` and custom-horizon `FAST_CANDIDATES`.
- `kalman_llt` — Kalman local-linear-trend state-space forecaster on log prices (statsmodels UnobservedComponents), the engine behind the various "Kalman predictor" scripts. In `CANDIDATES`.
- Monte Carlo odds — `models/tvinspired.mc_probabilities`: moving-block bootstrap (block 5, 2000 paths, fixed seed) over historical log returns; the custom-horizon response gains `monte_carlo: {p_up, p_gain_over_cost, p_loss_over_cost, sim_p05_pct, sim_median_pct, sim_p95_pct, n_paths}` and the decision note cites the cost-clearing odds. Bootstrap (not GBM) keeps fat tails and volatility clustering.

## Addendum 7 — Tehran-exchange gold funds (2026-07-21)

**Source.** Gold investment funds ("boxes": عیار/Ayar — instInfo 34144395039913458 —, طلا/Lotus, کهربا/Kian) quoted on TSETMC. Direct tsetmc.com access is geo-blocked outside Iran, so the `tse_funds` provider (migration 0009, category `iran_fund`, dormant without `BRSAPI_KEY`) reads BrsApi's TSETMC mirror `Api.BrsApi.ir/Tsetmc/Symbol.php?key&l18=<ticker>`. Configured via `TSETMC_FUNDS` (`ticker:SYMBOL,...`).

**Symbols.** `IR_GOLD_FUND_AYAR` / `_TALA` / `_KAHRABA` (unit price, rial→toman, unit `unit`) and the composite `IR_GOLD_FUND_FLOW` (currency `PCT`): volume-weighted retail net flow `(ΣBuy_I_Volume − ΣSell_I_Volume)/Σtvol × 100` across configured funds — positive = individuals net-buying from institutions. `observed_at` derives from the API's Jalali date + Tehran time (converted; dedupes naturally after close). FLOW is exempt from jump/MAD suspect tests (`OSCILLATING_SYMBOLS`) — sign flips are normal and a single-source symbol can never be second-source confirmed; sanity bounds ±100 still apply.

**Calendar.** New TSE class in both market-hours implementations (prefix `IR_GOLD_FUND`): Sat–Wed `MARKET_TSE_OPEN`–`MARKET_TSE_CLOSE` (default 12:00–18:00 Asia/Tehran), closed Thursday AND Friday (unlike the physical market, which trades Thursday). Collect job `funds`; freshness follows the standard closure rules.

**Features.** `compute_feature_frame` gains `gold_fund`/`fund_flow` inputs → `fund_ret_1`, `fund_ret_5`, `fund_ratio_z_30` (fund/physical relative-valuation z-score), `fund_flow`, `fund_flow_ma5`, `fund_flow_chg_5`. Wired into tabular models via `CONTEXT_SYMBOLS` (point-in-time truncated per fold) and into `feature_snapshots`.

**Serving.** Go `KnownSymbols` includes the fund symbols (prices/history/current endpoints serve them); the Trade panel shows a "Gold funds" card (prices + 24h change + retail net flow).

## Addendum 8 — multi-symbol forecasting + funds panel (2026-07-21)

**Multi-symbol core.** Migration `0010` adds `model_versions.symbol` (default `IR_GOLD_18K`; unique key now symbol+horizon+model+version). `FORECAST_SYMBOLS = (IR_GOLD_18K, XAUUSD)`: train/predict loop over both (bodies accept optional `"symbols": [...]`), artifacts under `MODELS_DIR/<symbol>/<horizon>/`, XAUUSD gets no Iranian exog context. `app_settings['live_calibration']` is now nested `{symbol: {horizon: stats}}`; ensemble live re-weighting filters by symbol; the meta-gate pools all symbols. Legacy flat summary keys mirror the primary symbol. Go: `GET /api/v1/predictions[?symbol=]` and `/predictions/{horizon}[?symbol=]` (default `IR_GOLD_18K`); `model_versions` responses include `symbol`. Signals, provider-gap widening, and custom horizons stay Tehran-18k-only by design.

**Funds panel.** `GET /api/v1/market/funds` aggregates the stored TSETMC payloads: per fund the latest price (rial→toman), Δ vs previous session close, volume/value, retail buy/sell % of volume, today's session averages of both, snapshot count, and buyer power (per-capita retail buy ÷ per-capita retail sell volume — قدرت خریدار حقیقی); plus current composite retail net flow and its 30-day daily history, and the TSE market state. Rendered as the "Gold funds" panel on Overview; the Forecast page gains a Tehran-18k ⇄ Global-XAU toggle (USD formatting for XAU).

## Addendum 9 — literature-driven upgrades (2026-07-22)

From the Array 2025 DL-for-trading systematic review (S2590005625000177) and Nature s41598-024-69325-3 (EvoLearn), transplanted to the deliberate sklearn/statsmodels stack:

- **GARCH-lite conditional volatility features** (after the review's hybrid LSTM-GARCH finding): RiskMetrics EWMA variance (`garch_vol`, alpha 0.06 ≈ λ0.94) and `garch_vol_ratio_60` (vol vs its 60-step norm) give every tabular model an explicit volatility state.
- **Denoised momentum** (`ret_med_5`, after wavelet-denoising stages à la Bao et al., dependency-free): rolling median of returns strips one-day spikes.
- **`hist_gb_tuned` candidate** (EvoLearn's core idea, sklearn form): randomized search over 6 HistGB configs, run ONCE on the earliest walk-forward window (train-only information, `reuse_across_folds` like ARIMA order selection), fitness = 1/(MSE_train + MSE_val) so the winner generalizes; params frozen across folds. Roster now 18 candidates.
- Deliberately not ported: RL agents, GNNs, sentiment feeds, deep architectures — the review's own flagged failure modes (overfitting, interpretability, compute) are what the naive-gated tournament exists to avoid.

**UI**: numeric spans (`.mono/.delta/.stat-value/.ticker-value/.big-price/.num`) get `unicode-bidi: isolate` — the RTL word تومان adjacent to LTR percents was visually reordering them (e.g. "+0.67%" → "0.67+ %").

## Addendum 10 — Hamrah Gold primary source (2026-07-23)

Migration `0012` inserts provider `hamrahgold` (category `iran_gold`, **priority 1**): the public unauthenticated ticker of the 24/7 Hamrah Gold platform (`pwa.hamrahgold.com/api/v1/market/price/xau/changes?type=sell|buy`, rial/gram). The observation is the buy/sell **midpoint** (sides + spread in `raw_payload`); keyless, honest UA, standard retry rules. Milli Gold falls back to second (priority 5), TGJU third. ~~The IR_GOLD_18K market calendar keeps the Iranian Thu+Fri off-days~~ — superseded by Addendum 11: 18k is always open.

## Addendum 11 — always-open 18k/USD, BitMax USDT provider (2026-07-23)

**Calendar.** `IR_GOLD_18K` and `USD_IRT` move to an ALWAYS-OPEN calendar (`ALWAYS_OPEN_SYMBOLS` / `alwaysOpen` in the Python/Go mirrors): open every hour of every day, Thursday and Friday included, because both primary sources quote continuously (update frequency drops on off-days, which the plain STALE_MINUTES rule handles honestly). They never enter a closure (`closure_started_at` → None / `ClosureStartedAt` → `at`); the closed-market freshness grace no longer applies to them. `MARKET_USD_OPEN` is removed from config/env. `IR_COIN_EMAMI` remains the only `MARKET_TEHRAN_*`-windowed symbol.

**Provider.** Migration `0013` inserts provider `bitmax`.

## Addendum 12 — live trading cost, performance fix, Brief tab (2026-07-23)

**Live trading cost.** `GET /api/v1/market/summary` gains `trading_cost_pct`: the latest observed Hamrah Gold buy/sell spread (from `raw_observations.raw_payload->spread_pct`, ≤3 days old; null otherwise). This replaces the frontend's fixed 1.5% round-trip assumption as the tilt cost basis (`effectiveCostPct`: live spread when in [0.1, 10]%, else the 1.5% fallback). Rationale: the observed dealer spread is ~0.5%, so the fixed 1.5% bar was triple the real cost and pushed every tilt to "favors waiting". The Advisor and Action-planner label the basis ("live dealer spread" vs "assumed").

**Models performance fixed.** `GET /api/v1/models/performance` previously returned `horizons` as a map keyed by horizon with symbol collisions (18k and XAUUSD rows overwrote each other) and field names the frontend never read — the panel always showed "No performance data". Now: `?symbol=` param (default `IR_GOLD_18K`), both the active-model and live-accuracy queries filter by symbol, and `horizons` is an ARRAY of `{horizon, symbol, model_name, version, metrics, baseline, live_accuracy:{n, mape_pct, directional_accuracy, interval_coverage}}` in canonical horizon order. Frontend Models page gains symbol chips (Tehran 18k / Global gold), reads `is_active` (the field the API actually emits; `active` kept for older payloads), and recognizes the `succeeded` training status.

**Brief tab.** New `/brief` page (see Addendum 12 body below).

## Addendum 13 — end-to-end review fixes (2026-07-23)

**Quant/ML correctness.**
- The buy/hold/sell signal's forecast inputs are now symbol-scoped: `_load_latest_predictions` filters `symbol='IR_GOLD_18K'`. Previously XAUUSD rows (written after gold rows every cycle) overwrote the per-horizon map and the Tehran signal was scored from global-gold forecasts.
- Maturity matching floors its search window at `predicted_at`: a prediction can no longer mature against an observation taken before (or at) the moment it was made — the old 36h window on a 24h horizon could score a forecast against its own base price during collection outages, silently corrupting live calibration, the meta-gate, and ACI.
- `hist_gb_tuned` gains `__setstate__`: unpickled artifacts refit with their TUNED hyperparameters; previously the predict-time refit silently fell back to the default estimator config.
- Plain `hist_gb` disables early stopping: sklearn's random validation split leaks overlapping h-step future returns into the stop decision.
- Live ensemble re-weighting only uses matured rows from the last 120 days (member evidence from different eras/regimes is not comparable).
- `eod` targets the Tehran end of day (23:59:59 Asia/Tehran), not UTC midnight (03:29 next Tehran morning).
- Custom-forecast round-trip cost now matches backtest/signals exactly (`2·fee + spread + 2·slippage`), and passes real input freshness to the meta-gate instead of hardcoded `True`.
- Failed TSE-funds fetches consume their quota slot (`app_settings['tse_funds_last_attempt']` marker): a broken key/mirror no longer burns the ~10/day BrsApi budget on every collect tick.
- Stale `training_runs` stuck in `'running'` >3h are reaped as failed at the next train start (container kill mid-run left them "running" forever).

**Security.**
- nginx overwrites `X-Forwarded-For` with `$remote_addr` and clears `True-Client-IP`: clients could previously spoof a fresh IP per request, fully bypassing the login rate limiter (unbounded credential stuffing) and forging audit-log IPs.
- Bearer tokens are checked against live user state (`auth.VerifyAgainstDB`): the role comes from the DB and deleted users are rejected immediately. Previously a deleted account kept access and a demoted admin kept `/admin/*` for the full JWT TTL.
- Closed-registration requests are rejected before any table lock (unauthenticated clients could serialize `users` via `LOCK TABLE`).
- Duplicate-email detection uses SQLSTATE 23505 (`pgconn.PgError`) instead of error-string matching.

**Operations.**
- Scheduler jobs carry per-job timeouts; `train` gets 90min (a run takes 30–36min; the old global 10-min context cancelled the HTTP call mid-run so Go recorded `job_failed` nightly while Python kept training). Redis lock TTL = job timeout.
- Redis runs `maxmemory-policy noeviction` (LRU could evict held scheduler locks → duplicate job runs). All containers get bounded json-file logging (20m×5).
- Migration `0014`: indexes `raw_observations(provider_code, observed_at DESC)`, `raw_observations(collected_at)`, `predictions(symbol,horizon,predicted_at DESC)`, partial `predictions(symbol,horizon,target_time DESC) WHERE actual_value IS NOT NULL` — the summary spread lookup, funds queries, retention delete, and all symbol-filtered prediction reads previously seq-scanned.
- Frontend image builds with `npm ci` against the committed lockfile; `lightweight-charts` pinned exactly. GitHub Actions CI added (go vet+test, pytest, tsc+vitest+build).

**Frontend.**
- Alert events read `triggered_at` (the field the API actually emits) — timestamps showed "never".
- Trade-panel candles keep the chart mounted through the 60s auto-refresh (zoom/pan no longer reset every minute).
- Chart Y-axes and the candle price scale honor the IRT/IRR toggle (`formatCompactToman`); previously axes stayed in toman while tooltips converted — a 10× on-screen discrepancy in rial mode.
- Users page no longer fires a guaranteed-403 request for non-admins; volatility card retitled honestly ("per step", it was never annualized); Brief says "assumed estimate" when no live spread exists; portfolio default date uses the Tehran day; Drivers' premium fallback average skips null rows; dead `register()` removed; dialogs get Escape-to-close + initial focus; Issues rows are keyboard-toggleable. the dashboard condensed into a written page — "Where things stand" (price, dollar, global gold, premium vs 30-day norm, live cost), "What the models expect" (per-horizon point + 90% range), "Possibilities and odds" (Monte Carlo from `GET /predictions/custom?days=7`: p_up, p(gain>cost), p(loss>cost), p05/median/p95; plus the cross-horizon interval envelope), and "Bottom line — the prescription" (tilt census net of the live cost, best buy/sell windows, the custom model's lean, stale-input and provider-gap cautions, disclaimer). Pure client-side composition (`lib/brief.ts`) over the same payloads the charts use. (category `fx`, **priority 1**, keyless): the public unauthenticated watcher of the BitMax exchange, `GET https://api.bitmax.ir/watcher/price/alternative`, parsed from `message.USDT.price_in_irt` (TOMAN per USDT despite the IRT label — verified against the rendered page). Emitted as `USD_IRT`: the 24/7 tether/toman rate is the documented proxy for the free-market dollar; its small visible premium over cash dollars is genuine market information. `raw_payload` keeps `price_in_usd`, 24h `change`, and `volume_24h_irt`. Existing FX providers fall back by priority.
