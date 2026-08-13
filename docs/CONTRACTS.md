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

## Addendum 15 — quant correctness, honest intervals, unified cost (2026-07-25)

**Measured outcome (training run 18, 36m57s).** With the significance gate live, **1 of 14** symbol-horizons runs a non-naive model: `IR_GOLD_18K/3d` -> `hist_gb_tuned`, holdout sMAPE 2.3996 vs naive 2.5698 (6.6% edge), MASE 0.972. Everything else is naive. This replaced 8/14 non-naive winners that had been selected by bare `argmin` over 18 candidates without multiplicity control. Run 17 (the same code minus the holdout materiality bar) additionally activated `XAUUSD/1d+eod` `extra_trees` on a **0.0004 sMAPE** holdout edge (1.47963 vs 1.47999 over 12 folds, MASE 1.004 - worse than naive in absolute-error terms); the bar now applies to both legs and MASE >= 1 is an outright veto.

**Known consequence: intervals are now much wider.** Valid 90% coverage from ~12 holdout residuals requires the `ceil((n+1)(1-alpha))`-th order statistic, which at n=12 is the LARGEST observed residual - so the band is set by the worst day in the holdout window. Observed live: ±1.94% (1h), ±9.43% (1d), ±16.44% (3d), ±36.40% (30d) for Tehran 18k. These are honest, not inflated: the previous narrow bands measured 0.72-0.78 realized coverage against a nominal 0.90. The path to tighter-AND-valid bands is more residuals (more folds, or a bias-corrected pooling of selection folds), not a return to interpolated quantiles. The live ACI loop will tighten them if observed coverage runs above target.

An empirical six-lens audit (agents ran probes, not just code reading) drove this pass. Findings are stated with the measurement that proved them.

**Intervals were structurally ~78%, not 90%.** `empirical_interval` used `np.quantile`, which interpolates between order statistics and carries no finite-sample guarantee. Monte-Carlo under perfect iid exchangeability measured realized coverage of the nominal-90% band at **0.72 / 0.76 / 0.77 / 0.78 for n = 8 / 10 / 11 / 12 residuals** — exactly the pool sizes this pipeline produces. `app/models/intervals.py` now implements split conformal (Vovk; Lei et al. 2018): the `ceil((n+1)(1-alpha))`-th order statistic. Three regimes — signed two-sided (n ≥ 19, preserves skew), symmetric on |residual| (n ≥ 9, valid), and flagged extrapolation below that (`coverage_guaranteed: false`) rather than silent interpolation. Re-measured coverage: **0.90–0.94**. ACI still runs on top, but it corrects *drift*, not the estimator bias that made the level dishonest. `walk_forward_coverage` returns `None` (not `0.0`) when nothing could be scored.

**Fold embargo.** Walk-forward spaces folds `step` apart while targets lie `horizon_steps` ahead, so adjacent folds shared future data whenever `step < horizon_steps` (measured 30× target overlap at n=120, h=30). `split_folds` now drops `ceil(horizon_steps/step)-1` folds between selection and holdout, sized from the real fold geometry (`_fold_step`).

**Significance gating.** `argmin` over 18 candidates is a multiple-comparison machine. A non-naive winner must now clear (1) `MIN_EDGE_PCT = 2%` relative sMAPE improvement, (2) a paired bootstrap CI over per-fold sMAPE differences (folds matched by `t_index`), (3) the embargoed holdout, and (4) a degeneracy check — a candidate whose folds merely reproduced naive (tabular models silently fall back until they have enough clean rows; measured bit-identical to naive until n_train ≥ 89 at h=30) is rejected as "not exercised". Rejection reasons are persisted. `MASE` added to `fold_metrics` as a scale-free comparator.

**Deployed artifact ≠ validated model (fixed).** `hist_gb_tuned` re-tuned on the full series at build time, whose tuning-validation tail overlapped the certification holdout by ~198 rows, then shipped carrying metrics measured for a *different* configuration. `_build_final_model` now calls `prepare_params(series[:selection_end])`, so hyperparameters are fixed on the selection prefix only; the tuning split additionally embargoes `horizon-1` rows whose labels resolve inside the validation block. `TUNE_MIN_ROWS` lowered to 24 so tuning is actually reachable at the first fold (it never ran before, making the candidate a byte-identical duplicate of `hist_gb` — which also gave that single model double weight in the ensemble; identical members are now deduplicated).

**Train/serve alignment.** (a) Models are fitted on complete bars but were served the still-filling bucket (at 00:05 UTC the newest daily bar is a 5-minute stub); measured shift in expected move 0.13–0.16pp, larger than the 0.15% flat band, so the direction call depended on which hour the cron fired. `_drop_incomplete_bar` serves complete bars only, while `expected_change_pct` is measured from the **live** price so no freshness is lost. (b) `_target_time` counted wall-clock while walk-forward counts observed bars: `eod` was trained ~27–46h out but graded 5–18h out, and XAUUSD's 30-step model is validated over ~35 calendar days. `_bar_aligned_target` derives the target from the series' own median bar spacing.

**One cost, everywhere.** `app/core/costs.py` is the single source of truth: the observed dealer buy/sell spread (≤72h old, sanity-banded) else a flagged assumption. Previously four hurdles coexisted — signal 2.2%, custom forecast 2.2%, backtest 1.65%, UI live spread ~0.5% — so the headline signal said "below the ~2.2% cost threshold" for moves the planner on the same page called "favors buying". Verified live: `(0.5156, 'observed_spread')`.

**Signal scoring.** The forecast contribution was non-monotonic — a +0.30% forecast scored *worse* than −0.30%, with a ~12-point cliff at the threshold, so a 0.02pp drift flipped hold→near-buy. It is now the continuous cost-adjusted edge with a symmetric deadband. With no forecasts available the signal declares itself **technical-only** instead of implying model backing at a fabricated 0.5 confidence.

**Monte Carlo conditioning.** Odds were bootstrapped from historical returns only — identical whether the model forecast +5% or −5% — yet rendered as the forecast's odds. `mc_probabilities` now recenters the simulated distribution on the point forecast (bootstrap supplies the shape, the model supplies the center) and reports `conditional_on_forecast`.

**Exogenous backfill.** Macro symbols had ~5 days of history against ~1200 for the forecast symbols, making macro features untrainable. `POST /internal/backfill/history` pulls multi-year daily bars from the public Yahoo chart endpoint; **5,011 rows inserted** (DXY, BRENT_OIL, XAGUSD, US10Y back to 2021-07). Daily closes are stamped 23:00 UTC on the bar's own date: at/after every handled instrument's session close (no look-ahead) yet inside the bar's own UTC day (no bucket shift). The legacy seeder's 12:00 UTC convention overstated availability by ~9h. `GET /internal/data/coverage` reports per-symbol depth.

**Frontend.** The tilt tooltip now derives from `horizonTilt` instead of re-deriving the branches — they contradicted each other for `-cost < pct < -cost/2` (badge "favors selling", tooltip "would lose money").

**Testing.** `scripts/pytest_docker.sh` runs the suite in a container built from the production image (the pinned deps target Python 3.12; newer hosts cannot build pydantic-core/psycopg wheels).

## Addendum 14 — holdout scoring, honest meta-gate, ops hardening (2026-07-23)

**Holdout-scored tournament.** `evaluate_candidates` splits each candidate's walk-forward folds chronologically: the first ~70% ("selection") drive candidate ranking and ensemble membership/weights; the last ~30% (≥5 folds; requires ≥15 total, else legacy all-fold behavior) are held out. `select_winner` picks on selection folds and CONFIRMS on the holdout — a non-naive winner that fails to beat naive out-of-sample falls back to naive. Stored `metrics`/`baseline_metrics`, the `MODEL_SMAPE` gauge, `models_evaluated` and the custom-forecast metrics all report holdout numbers (`report_metrics`; `params.holdout_scored` marks which). Interval `residual_pcts` come from the winner's holdout folds when ≥8 exist. Rationale: the min of ~18 noisy sMAPEs is optimistically biased; the ensemble previously entered the tournament with weights fit on the folds it was scored on. Expect stored sMAPEs to rise slightly after the first retrain — they are honest now, not worse.

**Meta-gate.** Migration `0015` adds `predictions.raw_confidence` (pre-gate confidence). The gate trains on it (fallback: blended value for old rows) so its own output no longer feeds back into its features; a new `is_global` feature separates IR_GOLD_18K from XAUUSD hit-rate structure; `apply_meta_gate` takes `symbol` and refuses to score with a gate whose stored `feature_names` don't match the current set (stays silent until the next refit).

**Artifact pruning.** The cleanup job deletes `.joblib` files under `MODELS_DIR` that are not referenced by any ACTIVE `model_versions.artifact_path` and are older than 14 days (summary key `pruned_model_artifacts`).

**Ops.** Per-account login lockout (5 consecutive failures in 15 min → locked 15 min → HTTP 429 `account_locked`; success resets; in-memory, single-replica by design) independent of the IP limiter. `make migrate-force VERSION=n` (api binary flag `-force-migration-version`) recovers a dirty migration state; procedure documented in troubleshooting.md. `make backup` / `scripts/backup.sh`: pg_dump custom-format dumps into `./backups/` with 14-day retention and optional off-host `rsync` via `BACKUP_RSYNC_TARGET`; cron line in the script header (installed on the production host).

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

## Addendum 20 — multi-timeframe MA trend alignment (2026-08-12)

**What it is.** A technical indicator, not a forecast. Three moving averages (default EMA 26/48/220) are evaluated *independently* on the 1D, 4H and 1H candle series of one symbol. A timeframe is `bullish` only under the strict stack `price > ma26 > ma48 > ma220`, `bearish` only under the strict mirror, `neutral` otherwise, and `unavailable` when warm-up, a gap or staleness makes the question unanswerable. Overall `alignment` is `full_bullish` / `full_bearish` only when all three agree; any `unavailable` timeframe blocks alignment outright. Comparisons are strict: two equal MAs are a crossover in progress, not a trend.

**Boundary.** Nothing here reaches model input, model selection, prediction confidence, intervals, or the buy/sell decision policy. The Overview card sits beside the advisor, never inside it, and never overrides "favors buying/selling/waiting/unclear/no call". `TREND_ALIGNMENT_ENABLED` gates the whole subsystem.

**Closed candles only.** The official state is read off the last *completed* candle of each timeframe (`start + width <= now`). A tick inside the forming candle cannot flip it — an alert fired on an unfinished candle can un-fire, which is worse than no alert. Buckets are floored in UTC epoch space; flooring in Tehran local time would land 4H boundaries on `:30` marks.

**Candle basis (a real limitation, stated).** `prices` holds ticks, not exchange OHLC. The Python job re-derives the same buckets `backend-go/internal/prices/candles.go` uses (`date_trunc`, `quality='ok'`, first/max/min/last), so the indicator and the chart agree. Only day/hour truncations exist, so **4H is resampled from hourly** (true OHLC: first open, max high, min low, last close). At current density the 4H EMA220 window reaches back ~45 days into the seeded-history era, where the DB holds roughly one point per day — the oldest part of that window is therefore daily-resolution, not true 4-hour buckets. 1D and 1H are unaffected.

**Events and idempotency.** *Being* aligned is not an event; *becoming* aligned is. A `trend_alignment_events` row is written only when the **stored** alignment differs from the new one and the new one is full. Leaving an alignment updates state but records no event. The duplicate guard is the database, not the comparison: `uq_trend_alignment_event_identity (symbol, alignment, latest_1d_candle_close, latest_4h_candle_close, latest_1h_candle_close)` with `ON CONFLICT DO NOTHING RETURNING id`, so a re-run, a racing second replica, a restart or a restored backup all converge on one event per (symbol, direction, closed-candle triple).

**Alerts.** `alerts.alert_type = 'trend_alignment'` is an opt-in subscription like every other alert; the Python job fans an entry out to enabled rows and writes `alert_events`. Go's `AlertTypes` accepts the type so it can be created, but `alerts.Evaluate` deliberately has no case for it — only the prediction service builds the MA state, and an unknown type falls through to "did not trigger". No subscriber means no alert row; the event is still recorded, which is what the UI reads. `cooldown_minutes` is not applied: entries are already unique per closed-candle triple, so a cooldown could only suppress a genuine entry.

**Migration `0019`** adds `trend_alignment_states` (one row per symbol, `previous_alignment`, `timeframes` JSONB, per-direction `last_*_alert_at`, `state_version`) and `trend_alignment_events` plus the unique identity index and `(symbol, occurred_at DESC)`.

**Job.** Go scheduler job `trend-alignment` (`SCHEDULE_TREND_ALIGNMENT_CRON`, default `7 * * * *`) calls `POST /internal/trend-alignment/evaluate`. Its own cron, own lock, own timeout: a failure here must leave collection, prediction and training untouched. Each symbol is evaluated in its own transaction, and the contained alert write runs inside a SAVEPOINT so a failing notification cannot roll back the record it notifies about.

**Read API.**
- `GET /api/v1/market/trend-alignment?symbol=IR_GOLD_18K` → `symbol, alignment, previous_alignment, timeframes{1d,4h,1h}, ma_type, periods, data_fresh, calculated_at, last_transition_at, last_alert_at, note`. Never-evaluated returns 200 with `note="never_evaluated"` and null measurements rather than 404. Each timeframe leg carries `trend, price, ma26, ma48, ma220, candle_open_time, candle_close_time, confirmed, data_fresh, ma_type, history_points, reason`.
- `GET /api/v1/market/trend-alignment/events?symbol=&limit=20` → `{items, count}`, limit ≤ 100, `occurred_at DESC`.
Go is a pure reader: it re-projects stored measurements and computes no MAs.

**UI.** Compact card on Overview, detailed per-timeframe table on Technical. State is conveyed by glyph **and** word (`▲ BULLISH`, `▼ BEARISH`, `● NEUTRAL`, `— UNAVAILABLE`), never colour alone; every row shows the candle close it was read from, and `reason` explains an `unavailable`. The frontend performs no EMA arithmetic — it renders what the API stored.

## Addendum 21 — candle feed v2: arbitrary intervals, pagination, honest OHLC (2026-08-12)

**What `prices` actually holds.** Ticks, not exchange OHLC. Every candle this API returns is synthesized by bucketing ticks, and the two consequences are reported rather than hidden. `IR_GOLD_18K` has exactly one observation per day from 2022-04-20 to 2026-07-19 and one every ~5 minutes from 2026-07-20 (measured inter-tick p50 300.0s, p90 300.5s), so 1198 of 1224 daily buckets have `open == high == low == close`; `XAUUSD` is the same shape. There is no volume column anywhere in `prices`.

**`GET /api/v1/market/candles`** — query: `symbol` (default `IR_GOLD_18K`, must be in the canonical symbol set), `interval` (default `1d`), `limit` (default 500, 1..2000), `before` (RFC3339 **or** unix seconds; the pagination cursor), `from`/`to` (explicit window, same two formats), `overlays` (`1` default, `0` for cheap history pages), `days` (legacy). Invalid values are **refused with 400**, never clamped — the `/intelligence/news` rule, for the same reason: a caller that asked for 15m and silently received 1h cannot tell its chart is wrong.

**Canonical intervals.** `5m 10m 15m 20m 30m 45m 1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 1w`. `daily` → `1d` and `hourly` → `1h` remain accepted; the response always echoes the **canonical** name, so a client that branched on `interval === 'hourly'` must branch on `interval_seconds` instead. `days` still works and keeps its old lenient clamp: it is converted to a bucket count (`ceil(days*86400/interval_seconds)`, capped at 2000), which reproduces the old windows exactly — `interval=daily&days=120` → 120 buckets, `interval=hourly&days=14` → 336. An explicit `limit` wins over `days`.

**Bucketing.** UTC epoch floor — `to_timestamp(floor(extract(epoch from observed_at)/N)*N)` — for every sub-day interval and for `1d`/`2d`/`3d`; `date_trunc('week', …)` (Monday 00:00 UTC, the chart convention) for `1w`. `date_trunc` is deliberately not used below a day: it only supports hour and day, and arbitrary timeframes are the point. `2d`/`3d` boundaries are epoch-aligned, not calendar-aligned. Aggregation is `quality='ok'` only, open = first value by `(observed_at ASC, id ASC)`, close = last by `(observed_at DESC, id DESC)`, `max`/`min` for the extremes, `ticks = count(*)` — the same buckets `prediction-python/app/jobs/trend_alignment.py::_load_candles` builds, so the chart and the indicators drawn on it cannot disagree. Tehran/Jalali is display-only and never touches bucketing.

**Response.** `symbol, interval, interval_seconds, timezone:"UTC", candles[], has_more, next_before, effective_window, coverage, overlays, pivots, support, resistance, as_of`. Each candle: `t` (unix seconds, bucket start), `open_time`, `close_time` (exclusive end = start + `interval_seconds`), `open, high, low, close`, `volume`, `ticks`, `confirmed`, `synthetic`.
- **`synthetic`** = `ticks <= 1`. The honesty flag: a bucket built from one tick has no observed range — its high and low are that one price repeated — and the chart renders those bars differently instead of implying a range that never existed.
- **`confirmed`** = `bucket end <= now`. The forming candle is still returned (a chart needs its live bar) but is labelled, because a value read off an unfinished bucket can still move. Same rule as the trend-alignment engine's `start + width <= now`.
- **`volume` is always `null`.** There is no volume data; a synthesized figure would be read as a measurement.
- `pivots` and `support`/`resistance` are computed from the **full fetched array including warm-up**, not from the returned page — otherwise `?limit=3` (exactly what the chart's live poll sends) produced a confident support level from a 3-bar lookback instead of the 20 the indicator asks for. A fetch genuinely short of the 20-bucket lookback returns `null` rather than a clamped number. `pivots` come from the newest **confirmed** bucket (classic pivots are defined on a completed bar). `overlays` is `null` when `overlays=0`.

**Pagination.** The newest ≤`limit` buckets at or before the cursor, returned **oldest-first**. `next_before` is the oldest returned bucket start; the client passes it straight back as `?before=`. **The requested window is snapped OUTWARD to bucket boundaries** — `from` floors down, `to` ceils up — and the snapped bounds drive both the bucket selection and the `observed_at` predicate, so every returned bucket is whole. `before` still floors down and stays exclusive: it names a bucket the caller already holds, and ceiling it would re-serve that bucket on every page. `to` stays exclusive after snapping so adjacent windows tile instead of overlapping by a bar. The snapped bounds are echoed back as `effective_window {from, to}`. Two defects made this rule necessary, both reproduced before the fix: `?from=…T00:00Z&to=…T23:59:59Z&interval=1d` returned zero candles for a day that had data, and an unfloored `from` returned a bar stamped as a whole day but aggregated from its last 10 minutes — with `synthetic` computed off that truncated count, so a 288-tick day could report `ticks: 2, synthetic: false` and a fabricated "daily range". `has_more` is exact: warm-up buckets trimmed off the front prove older history exists, and otherwise the answer comes from an index-only `EXISTS` probe rather than a second aggregation. With `from` set, "older" means older *inside the requested window*. Overlays are index-aligned with the returned candles and computed with 60 extra lead-in buckets (SMA50 needs 50, Ichimoku senkou B 52) that are **not** returned; `overlays=0` skips both the lead-in fetch and the arithmetic.

**`coverage`** — `base_granularity_seconds` (median inter-tick gap measured over the DENSE period, i.e. from `intraday_from` onward, snapped to the nearest conventional cadence; `null` when unmeasurable), `intraday_from` (start of the earliest day with ≥12 ticks in an unbroken run of such days reaching the present — the current UTC day is exempt from the threshold since it is still filling; `null` when density never reaches now), `history_from` (`min(observed_at)`), `supported_intervals`, `note`. `supported_intervals` is the canonical list minus every sub-1d interval when `intraday_from` is null, minus every **sub-day** interval shorter than `base_granularity_seconds`. The granularity filter deliberately gates sub-day intervals ONLY: it was originally measured over a trailing week and applied to the whole vocabulary, so a three-day provider outage pushed the median past ~2.65 days, left `["1w"]` supported, and made `GET /market/candles` **with no parameters at all** return 400 — refusing its own default interval for up to 10 minutes. A daily candle does not stop being computable because last week was quiet. The whole block is cached in memory per symbol for 10 minutes: it scans a symbol's entire history and the chart polls, so pagination pages must not each pay for it.

**Refusing a timeframe.** An interval outside `supported_intervals` returns **400** with message `"This timeframe is not available for the current data source."` and details naming the symbol, the requested interval and the supported list. The endpoint never substitutes a different interval.

**Boundary.** Candle synthesis and the indicator overlays are technical arithmetic over stored observations. Nothing in this endpoint touches model training, prediction, signals, calibration or the buy/sell decision policy, and Go computes no forecast.

## Addendum 22 — gate applicability by evidence, publishable interval coverage (2026-08-13)

Three defects in the Addendum-15/16 self-assessment and coverage work, all reproduced before being fixed.

> **Status (Addendum 24).** The horizon rule described in the next paragraph — score a day count only when it has at least `GATE_MIN_LEVEL_ROWS` matured predictions of its own — is what ships today. Addendum 23 replaced it with a nearest-neighbour borrow; Addendum 24 deleted the borrow and restored this, with measurements.

**The meta-gate's applicability check was anti-correlated with actual support.** `_support` treated `log_horizon_days` as continuous and refused at `|z| > 3`. That column is DISCRETE in training — the scheduler only ever emits `1h/4h/eod/1d/3d/7d/30d`, four distinct day counts once `eod` and `1d` collapse — while `POST /internal/predict/custom` accepts any integer 1..90 days. A marginal Gaussian cannot see the gaps between the modes: measured on a real 500-row fit (`1d`:194, `eod`:115, `3d`:89, `7d`:71, `30d`:31), **10d — zero training rows — scored `|z| = 1.62` and was accepted with no refusal and no warning, while 30d — the only horizon out there that HAS evidence, 31 rows — scored 3.35 and was refused.** Support for a discrete column is now decided by evidence: `fit_meta_gate` persists `horizon_rows`, the per-horizon row counts of exactly the rows that entered the fit, keyed by days (`"1.000000": 309` — `eod` and `1d` pool), and a horizon backed by fewer than `GATE_MIN_LEVEL_ROWS` (10, the same bar the indicator levels use) is refused however close its logarithm looks. The `|z|` rule now applies only to the three genuinely continuous features (`rel_width`, `abs_expected_pct`, `confidence`). A stored gate without `horizon_rows` is `unusable` — silent, like any other stale-schema gate — until the next evaluate run refits it (hourly, `SCHEDULE_EVALUATE_CRON`).

**The 2–3 SD shrink manufactured confidence out of a support failure.** `shrink = (|z|-2)/(3-2)` blended the score toward the gate's base rate, so at `|z| = 2.996` the published number was 99.6% a constant that contains nothing about the forecast — still `status: "scored"`, still a non-null `p_hit`, still blended 50/50 into shipped confidence. Measured on the production gate: a raw 0.076 (a pathologically LOW extrapolation) was published as 0.525, i.e. the substitution INVENTED confidence exactly where the evidence had failed, and one more day of horizon then flipped it to `None`. The shrink is gone. Inside the domain `p_hit` is always exactly what the fitted model computed; between `GATE_THIN_Z` (2.0) and `GATE_MAX_Z` (3.0) the verdict carries `thin_support: true` and the `self_assessment` driver says so in words. Nothing is silently substituted, and a decline is visible to every caller.

**`custom.py` publishes a `self_assessment` driver.** It called `apply_meta_gate` (a bare float), blended the result into shipped confidence and emitted **no** driver at all — on the path most likely to fall outside the gate's support, a reader could not distinguish a scored gate from a declined one from an absent one. It now takes the verdict, publishes `_gate_driver` for every outcome (`scored` / `out_of_support` / `untrained` / `unusable`), and raises the same below-coin-flip warning the scheduled path does. Driver notes name the offending feature in words ("this forecast horizon", "this interval width") rather than by column name.

> **Correction (Addendum 23).** This paragraph originally claimed `custom.py` was "now as accountable as the scheduled path". That was false and the next audit disproved it by execution: the driver was published, but the *number it described* was computed on a different scale from the scheduled path's, so the gate refused every custom forecast and the driver faithfully reported a reason that was the application's own fault. Publishing a reason is not the same as the reason being true.

**Interval coverage was structurally impossible to publish.** `MIN_SCORED_FOR_COVERAGE = 20` was correct; the plumbing made it vacuous. `report_metrics` prefers `holdout_metrics`, the holdout is `max(5, 30% of ≤41 folds) ≤ 12` folds, and `walk_forward_coverage` spends its first 10 folds building the residual pool — so **the block that actually gets stored could score at most 2 folds against a bar of 20, for every candidate at every horizon forever**, and reported `not_scored` for a model walk-forward validated on 30 folds. Interval coverage is a property of the interval CONSTRUCTION, measured walk-forward with no peeking, so it is now measured over the full walk-forward fold set (`fold_metrics(..., coverage_folds=folds)`) and published under its own name as one nested block:

```
"interval_coverage_walk_forward": {"rate": 0.93|null, "hits": 28, "scored_folds": 30,
  "total_folds": 40, "residual_warmup_folds": 10, "min_scored_folds": 20, "status": "measured"}
```

`rate` is still withheld (`null`) below `min_scored_folds`, with `status` = `measured` / `insufficient_evidence` / `not_scored` saying why — the honest-denominator rule is unchanged, it can now actually be met (real tournament: 40 folds → 30 scored → measured; a genuinely short 15-fold run → 5 scored → `insufficient_evidence`). The flat `interval_coverage*` keys are **gone**: a bare float sitting next to `n_folds` (which counts holdout folds, a different fold set) is exactly what made the original defect invisible. `total_folds` next to `n_folds` states the difference. Cost, stated: the full fold set includes the selection folds, which carry the winner's selection optimism — hence the name saying which walk it came from. Nothing else moved: model selection, activation gates, the naive-baseline gate, the ensemble and the conformal interval maths are untouched. Go's `minCoverageN` needs no change — it gates LIVE coverage over matured predictions, a different denominator, and stays at 20 so both services keep one evidence bar.

**Also fixed:** `_support` ran before any length check and `IndexError` was not caught, so a stored gate with a short `mean`/`std` vector and no `feature_names` crashed a whole prediction run; malformed gates are now `unusable`.

## Addendum 23 — one meaning for "confidence", ~~horizons that borrow their neighbour~~, a coverage rate the custom path can actually publish (2026-08-13)

Three more defects in the Addendum-22 work, all reproduced by execution before being fixed. Two of them were *created* by Addendum 22; the third it left standing while claiming otherwise.

> **Partly retracted (Addendum 24).** The confidence-scale fix and the `CUSTOM_MAX_FOLDS` fix below stand and still ship. The horizon-borrowing mechanism and the unbounded live-calibration fallback were themselves defects and have been deleted; both are struck through in place, with the measurement that disproved each.

**The two prediction paths disagreed about what `confidence` MEANS, and the gate refused every custom forecast because of it.** `predicting._predict_one` computes `blended_confidence(_confidence(dir_acc, rel_width), live_cal, regime)` and persists that POST-blend value as `raw_confidence`; `fit_meta_gate` therefore trains its `confidence` column on the BLENDED distribution. `custom.predict_custom` computed `_confidence(dir_acc, rel_width)` with no blend and handed that raw number to `score_meta_gate`, which z-scored it against the blended distribution and declined. Measured on an internally-consistent 500-row pool with live calibration present: the gate's `confidence` feature was **mean 0.6861, sd 0.0434**, the custom path shipped **0.95**, and the verdict was `out_of_support` at **|z| = 6.1** — on a 1-day horizon backed by **309** training rows. Every custom horizon was refused, `_support` takes the max over features so `confidence` refused first and the horizon check never ran, and the driver told the user *"this confidence level sits 6.1 SD outside the 500 past predictions it was fitted on"* about a completely ordinary forecast. The blend is what compresses the training spread (`w = max(0.3, 1 - n/60)`, so 60 matured predictions shrink it to 30% of its raw width); the control confirms it — with no `live_calibration` row the same custom calls score. `custom.py` now computes confidence through `blended_confidence` with the same inputs, via the shared `predicting.live_calibration_for`. Post-fix the same forecast lands at |z| = 1.9 and is scored.

> **Retracted (Addendum 24).** This paragraph originally introduced `predicting.live_calibration_for_days`, an *unbounded* nearest-horizon-in-log-days fallback, and argued the absence of a bound was principled because "the alternative to near evidence is not caution but shipping the uncalibrated validation number". The audit refuted it by execution: on a deployment whose first four hours have matured, a **90-day** forecast was calibrated on the **1-hour** directional hit rate — 2160× out — and since the blend weight floors at `w = 0.3`, **70% of that 90-day shipped confidence was the one-hour number**. The function is deleted. Both paths now use `predicting.live_calibration_for`, exact horizon only; when the requested day count has no matured predictions of its own the confidence is simply not blended, and the `confidence_calibration` driver says so and states that no other horizon's outcomes were substituted. That lookup also carries the pre-multi-symbol flat-layout fallback, which `live_calibration_for_days` did not — on a legacy `live_calibration` row the scheduled 7d forecast shipped 0.915 while the custom 7-day forecast shipped 0.950, the same class of split between the two paths that this addendum was written to close.

**~~Requiring exact horizon evidence relocated the one-day cliff instead of removing it.~~** *(This was miscalled a defect; the step it describes is the intended evidence boundary — see Addendum 24. What was genuinely wrong here was only the regression test.)* Addendum 22 refused any horizon with fewer than `GATE_MIN_LEVEL_ROWS` rows, so confidence was gate-blended at the four day counts the scheduler emits and untouched one day either side. Through the real `predict_custom`: **6d → 0.928, 7d → 0.704, 8d → 0.923** and **29d → 0.877, 30d → 0.734, 31d → 0.864**. Its regression test asserted only over `range(8, 25)`, where *every* day count is unevidenced, so it passed by construction and both real boundaries sat outside its window.

> **Retracted (Addendum 24).** The fix this addendum shipped for that defect was `GATE_HORIZON_BORROW_RATIO = 1.5`: a horizon with no rows of its own borrowed the nearest evidenced horizon in log-days. It is deleted, on two findings.
>
> First, it did not remove the step — it moved it to the borrow radius and made the largest one **worse than the tolerance the addendum itself set**. This addendum shipped with the claim that the remaining boundaries were "0.027 and 0.049 as measured"; that was true only of the two windows it swept. The audit measured **0.060 across 19d → 20d** against `MAX_ONE_DAY_CONFIDENCE_JUMP = 0.05`, flipping the published self-assessment from a refusal to a 0.89 endorsement on one extra day of horizon. That paragraph is deleted rather than struck through, because every number in it was window-conditional.
>
> Second, it bought smoothness with a partly fictional adjustment. A 20-day forecast scored on 30-day outcomes is not a measurement of 20-day skill, and disclosing the substitution does not convert it into one — the same objection this file makes to the base-rate shrink in Addendum 22.
>
> The original paragraph's own regression test was also unsound in the way it warned about: it swept exactly the two maximal scored islands (`range(1, 11)` and `range(20, 46)`), so "nothing in the window is declined" held **by choice of window**. Executed at the time: `range(1, 11)` passed and `range(1, 12)` failed, on excluded steps of 0.24 — nearly 5× the tolerance.
>
> What ships is Addendum 22's rule, unmodified: the gate scores a horizon it has evidence for and declines one it does not, and confidence is left untouched when it declines. See Addendum 24 for the boundary behaviour and the replacement test.

**The custom path could never publish an interval-coverage rate.** `CUSTOM_MAX_FOLDS` was 25 and `walk_forward_coverage` burns its first `min_history = 10` folds building the residual pool, so the block that ships could score at most 15 against `MIN_SCORED_FOR_COVERAGE = 20` — for every horizon and every candidate, forever. `interval_coverage_walk_forward.rate` was structurally `null`, with nothing next to it saying so. Measured cost of raising the budget (9 `FAST_CANDIDATES`, warm process):

| daily points | days | 25 folds | 40 folds | scored folds 25 → 40 |
|---|---|---|---|---|
| 300 | 7d | 8.7s | 11.6s | 14 → 29 |
| 300 | 30d | 6.4s | 9.4s | 14 → 26 |
| 700 | 7d | 9.3s | 14.9s | 15 → 30 |
| 700 | 30d | 8.9s | 14.0s | 15 → 29 |

`CUSTOM_MAX_FOLDS` is now **40**, matching the nightly `MAX_FOLDS`: +3 to +6 seconds on a request that already takes 6–9, against the Go client's 5-minute timeout. It is not a guarantee — a short history with a long horizon still produces fewer folds than the budget asks for — so when the rate is withheld the response now carries a warning naming the actual denominators (`total_folds`, `residual_warmup_folds`, `scored_folds`, `min_scored_folds`) instead of shipping a bare `null`.

**Over-refusal pin.** The scheduled path is untouched by all of this, and it is measured rather than asserted: 400 matured rows over 1d/3d/7d/30d, gate fitted, then the gate's own training rows and 4,000 fresh draws from the same distribution scored back through it. Refusal rate **4.00%** in-sample, **3.30%** out-of-sample, **4.15%** across all seven scheduled horizons — **identical before and after this round, to the row**. Refusals are on `abs_expected_pct` and `rel_width`, i.e. genuinely unusual forecasts. (Re-measured after Addendum 24 on the internally-consistent 500-row pool: **0.12%** over 2,500 ordinary scheduled forecasts, all three refusals on `confidence`, unchanged by the removal — a scheduled horizon always had its own evidence, so borrowing never fired there and deleting it changed nothing.)

Untouched, as before: model selection, activation gates, the naive-baseline gate, the ensemble, the conformal interval maths, and the Go service.

## Addendum 24 — consolidation: the gate scores what it has evidence for, and nothing else (2026-08-13)

Three rounds of work on the meta-gate each fixed a real defect and introduced a new one that only execution caught. This round removes rather than patches. Nothing new is invented; two mechanisms are deleted and the remaining behaviour is stated honestly.

**Kept, all independently re-verified:** `interval_coverage_walk_forward` published from the full walk-forward fold set with its denominator (`rate, hits, scored_folds, total_folds, residual_warmup_folds, min_scored_folds, status`), the flat `interval_coverage` key still gone; the evidence-based gate support check; `custom.py` computing confidence through `blended_confidence` so the gate sees the scale it was trained on; the `IndexError` length guard; `CUSTOM_MAX_FOLDS = 40`.

**Deleted: nearest-evidenced-horizon borrowing.** `GATE_HORIZON_BORROW_RATIO`, `nearest_evidenced_horizon`, `GateVerdict.borrowed_horizon_days`, `metagate.horizon_label` and the disclosure path in `predicting._gate_driver` are gone; `_support` returns `(max_abs_z, worst_feature)`. Reasons in the retraction under Addendum 23. The rule is now one biconditional, and the regression test states it that way: **the gate scores day count N if and only if N has at least `GATE_MIN_LEVEL_ROWS` matured predictions at exactly N days.** With evidence at 1/3/7/30 days, that is 4 scored day counts out of the 90 `/internal/predict/custom` accepts; the other 86 return `out_of_support` with `worst_feature = "log_horizon_days"` and confidence untouched.

**Deleted: the unbounded live-calibration fallback.** `predicting.live_calibration_for_days` is gone; both paths use `live_calibration_for`, exact horizon only. Reasons in the retraction under Addendum 23.

**The confidence step at an evidence boundary is intended behaviour.** The gate blends its learned P(direction hit) 50/50 into confidence at a horizon it has matured predictions for, and does not touch confidence at one it does not, so shipped confidence *necessarily* changes between 7d and 8d. Measured on the pure gate with a fixed forecast, the step is **0.244**; measured end to end through the real `predict_custom`, **0.147** (7d → 8d). That is not a defect to be smoothed. It marks the exact point at which this system stops having a measurement of its own skill, and both sides of it are published: the `self_assessment` driver says either `learned P(direction hit)=…` or `declined to score this forecast: this forecast horizon never appears in the N past predictions it was fitted on … confidence left untouched`, and the `confidence_calibration` driver flips between `calibrated against N matured 7d prediction(s)` and `not calibrated against live outcomes: no 8-day predictions of this system's own have matured yet … and no other horizon's outcomes were substituted for it`. Two attempts to make this step small both ended up producing a *larger* one somewhere else while claiming otherwise.

**The two paths are self-consistent about evidence, and it is asserted as a biconditional.** A day count is calibrated against live outcomes **if and only if** the gate scores it, because both facts come from the same place: matured predictions at exactly that horizon. This is what makes the exact-horizon calibration rule safe rather than merely strict — the gate's `confidence` column is fitted on blended values, so if a horizon could lose its calibration while keeping its gate score the gate would be reading a number off a scale it had never seen. Measured with the borrow rule artificially reinstated, that is exactly what happens: a 45-day request passes the (borrowed) horizon check and is then refused at **|z| = 6.1 on `confidence`** — the Addendum-23 defect, re-created by the Addendum-23 fix.

**The regression test no longer asserts that no step exceeds the tolerance.** Three rounds of cliff tests passed by construction — round 2 swept a window in which every day was unevidenced, round 3 swept exactly the two islands the borrow rule scored. The properties now asserted are falsifiable and cannot be satisfied by choosing the window: the sweep **must contain both a scored and a declined day, and an adjacent pair of them** (windows that do not are rejected before any step is measured — `range(8, 25)`, `range(31, 46)`, `range(4, 7)` and `range(1, 2)` all fail this guard); every step **larger than the tolerance must coincide with a change of gate status**; every such step must be **disclosed in the drivers** on both sides; and steps **within a run of same-status days must stay under the tolerance**. `MAX_ONE_DAY_CONFIDENCE_JUMP` is unchanged at 0.05 but now means "the most a forecast may drift without the published evidence behind it changing", not "the largest step the system may produce".

One extra clause exists in the end-to-end version, because one extra thing can legitimately move the number there: the custom path runs a model tournament per horizon, so the winning model can change from one day count to the next. Measured on the test fixture: 37d and 38d are both declined by the gate, yet shipped confidence steps **0.802 → 0.873**, because the winner changes from `linear` (`rel_width` 0.0788) to `ensemble` (0.0394). That is a different forecast, not a different rule, and `model_name` publishes it — so the end-to-end assertion is that every over-tolerance step is accompanied by a change the response itself reports (gate status **or** winning model), never by neither.

Untouched, as before: model selection, activation gates, the naive-baseline gate, the ensemble, the conformal interval maths, and the Go service. Nothing was retrained.

**Gate evidence is scoped to the INSTRUMENT, not just the horizon.** The biconditional above is only true because of this. The per-horizon counts are keyed `symbol|days` (`metagate.evidence_key`) and stored under `horizon_evidence`; a gate persisted under the older flat `horizon_rows` key is treated as **unusable** rather than reinterpreted, so it stays silent until the hourly evaluate job refits it — the safe direction. Without the scoping the count pooled every symbol, and an audit reproduced the consequence: with 60 matured XAUUSD 30d rows and **zero** IR_GOLD_18K 30d rows, a 30-day gold request was `scored` on another instrument's outcomes and shipped confidence moved 0.825 → 0.719. Worse, live calibration *is* per-symbol, so that same request was simultaneously "gate scored" and "not calibrated" — handing the gate an **unblended** confidence and re-creating the exact scale mismatch the blend exists to remove. The regression test builds that asymmetry (120 gold 1d + 60 gold 7d + 60 global 30d), asserts the gold 30-day request is refused on `log_horizon_days`, and asserts the global one is still **scored** — so it pins a scoping fix rather than a blanket refusal.
