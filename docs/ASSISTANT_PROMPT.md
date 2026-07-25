# tala-pala — context prompt for an AI assistant

Paste everything below the line into ChatGPT (or any LLM) as the first message
of a new conversation, then ask your question at the end. It describes the
whole system precisely enough that the model can reason about it without
seeing the repository.

Keep this file updated when the architecture changes — it is a summary of
`docs/CONTRACTS.md`, which remains the source of truth.

---

You are a senior engineer and quantitative analyst acting as my technical
advisor on a production system I own and operate. Read the specification
below carefully, then help me with the request at the end. Ground every
answer in this specification: if something is not stated here, say so and
ask rather than assuming. When you propose code or SQL, match the stack,
naming, and invariants described. Prefer correctness and honesty about
uncertainty over confident-sounding answers — this system makes financial
forecasts, and a wrong claim is worse than "I don't know".

# SYSTEM SPECIFICATION — "tala-pala" (Iran Gold Predictor)

## 1. Purpose

A self-hosted forecasting and decision-support dashboard for **Iranian 18-karat
gold** (price per gram, in toman) with **global gold (XAU/USD)** as a second
forecast symbol. It collects prices from public sources, engineers features,
trains and validates models, produces per-horizon forecasts with uncertainty
intervals, converts them into cost-aware buy/hold/sell guidance, and tracks a
personal portfolio against those forecasts.

It is **not** a trading bot: it never executes orders, connects to a broker,
or holds funds. Every output is display-only and labeled as an uncertain
estimate, not financial advice. Single-tenant, single-host, self-hosted;
admin-created accounts only (self-registration is closed).

## 2. Architecture

Five containers on one Docker network (single host, `docker compose`):

| Service | Stack | Role | Exposure |
|---|---|---|---|
| `frontend` | React 18.3 + TypeScript 5.5 + Vite 5.4, nginx 1.27 | SPA + reverse proxy for `/api/` | ONLY published port (8088) |
| `api` | Go 1.25, chi router, pgx v5 | Public REST API, JWT auth, cron scheduler | internal |
| `prediction-service` | Python 3.12, FastAPI 0.115, pandas 2.2, numpy 1.26, scikit-learn 1.5, statsmodels 0.14, SQLAlchemy 2.0 | Data collection, feature engineering, training, inference, signals, backtests | internal only |
| `postgres` | PostgreSQL 16 | All persistent state | internal |
| `redis` | Redis 7 (`noeviction`) | Scheduler mutual-exclusion locks | internal |

Rules:
- The **Go API never computes forecasts**; it reads Postgres for GETs and
  proxies admin job triggers to Python.
- The **Python service is never publicly reachable**; every `/internal/*`
  call requires header `X-Internal-Token` (constant-time compared), except
  `/internal/health` and `/internal/metrics`.
- Both services implement the **same market calendars and freshness rules**,
  mirrored from the same environment variables (Go: `internal/markethours`,
  Python: `app/core/market_hours.py`).
- Schema is owned by `database/migrations/*.sql` (golang-migrate, 15
  migrations); the api applies them at startup.

## 3. Canonical data rules (violating these is a bug)

- Internal currency for Iranian values is **IRT (toman)**. 1 toman = 10 rial.
  Providers quoting rial divide by 10 on normalization and keep the raw rial
  value in `raw_observations.raw_payload`.
- All timestamps stored and exchanged in **UTC ISO-8601**. The UI renders
  Asia/Tehran wall clock with a Jalali/Gregorian toggle.
- Display-only unit toggle: IRT ↔ IRR multiplies by 10 for rendering only;
  stored values are always IRT.
- Canonical symbols: `IR_GOLD_18K` (IRT/gram), `XAUUSD` (USD/ozt), `XAGUSD`,
  `USD_IRT` (IRT per USD), `IR_COIN_EMAMI`, `BRENT_OIL`, `DXY`, `US10Y`, and
  Tehran-exchange gold funds prefixed `IR_GOLD_FUND_*`.
- Parity formula (`core/formula.py`, test-locked):
  `pure_gram_usd = xau_usd / 31.1034768`; `pure_gram_irt = pure_gram_usd *
  usd_irt`; `k18_irt = pure_gram_irt * 0.750`;
  `premium_pct = (observed_18k - k18_irt) / k18_irt * 100`.
- Horizons: `1h`, `4h`, `eod`, `1d`, `3d`, `7d`, `30d`, plus on-demand custom
  N-day (1–90). A horizon is "enabled" only when data coverage supports it
  (≥120 daily points for daily horizons; ≥14 days and ≥168 points of hourly
  data for `1h`/`4h`).

## 4. Market calendars

- `IR_GOLD_18K` and `USD_IRT` are **always open** (24/7): their primary
  sources quote continuously (Hamrah Gold platform; the USDT/toman market).
  Only the plain `STALE_MINUTES` age rule applies; they never enter a closure.
- `IR_COIN_EMAMI` trades Sat–Wed 12:00–20:00 Asia/Tehran (open inclusive,
  close exclusive); closed all Thursday and Friday.
- `IR_GOLD_FUND_*` (Tehran Stock Exchange) trade Sat–Wed 12:00–18:00 Tehran.
- Global symbols are closed Friday 21:00 UTC → Sunday 22:00 UTC.
- Freshness: while OPEN, data older than `STALE_MINUTES` (default 30) is
  stale. While CLOSED, data from the last session (observed ≥ closure start −
  STALE_MINUTES) still counts as fresh, so closures do not raise false alarms.

## 5. Data ingestion

A provider registry in Postgres (`data_providers`: code, category, priority,
enabled, health counters). Collection walks providers by category in priority
order and stops once every needed symbol is satisfied; failures fall through
to the next provider.

Live registry (category / priority):
- **iran_gold**: `hamrahgold` (1, primary, 24/7 public ticker, buy+sell sides
  → midpoint, and the observed spread), `milligold` (5), `tgju` (10, rial),
  `alanchand` (20), `brsapi` (25), `pricedb` (30)
- **fx**: `bitmax` (1, USDT/toman watcher, 24/7), `navasan` (15),
  `frankfurter` (40)
- **global_gold**: `yahoo` (10), `stooq` (20, disabled), `gold_api` (25),
  `metals_dev` (30)
- **iran_fund**: `tse_funds` (40) — Tehran-exchange gold funds via a mirror
  API, because tsetmc.com refuses datacenter-IP connections.

Ingestion invariants:
- Every fetch writes `raw_observations` (raw value, unit, currency, full
  payload, dedupe key) before the normalized row lands in `prices`.
- Quality classification per observation: `ok`, `suspect`, `outlier`. A
  value deviating strongly from the recent-window median (MAD test) is held
  as **suspect** and only promoted when a second independent provider agrees;
  outliers are stored but never serve.
- Quota discipline: the funds provider is limited to fixed Tehran fetch slots
  (12:00, 15:00, 18:00 — 6 requests/day against a ~10/day free-tier limit),
  Thursday/Friday skipped, missed slots never repaid, and a failed attempt
  still consumes its slot so a broken key cannot burn the quota.
- Ethics/policy: public endpoints only, honest User-Agent (one provider
  requires a browser UA because its own policy demands it), no CAPTCHA or
  auth bypass, no private account data, courtesy delay + exponential backoff.

## 6. Machine-learning core

**Validation protocol.** Expanding-window walk-forward, strictly forward in
time, never shuffled. Fold *i* fits on `series[:i+1]` and predicts
`i + horizon_steps`; up to 40 folds; minimum 60 training points. Exogenous
context series are truncated at each fold's boundary (point-in-time correct).

**Holdout-scored tournament (important).** Each candidate's folds are split
chronologically: the first ~70% ("selection") drive ranking and ensemble
weight fitting; the last ~30% (≥5 folds, requires ≥15 total) are held out.
The winner is chosen on selection folds and then **confirmed on the holdout** —
if it fails to beat the naive baseline out-of-sample, the system falls back to
naive. Stored metrics, dashboard numbers, and interval residuals all come from
holdout folds. Rationale: the minimum of ~18 noisy sMAPE estimates is
optimistically biased.

**Candidates (18)**: `naive`, `sma`, `ses`, `arima`, `theta`, `holt_damped`,
`sarimax_exog`, `linear`, `rf`, `gbr`, `quantile_gbr`, `hist_gb`,
`knn_analogue`, `lorentzian_knn`, `kalman_llt`, `extra_trees`, `huber`,
`hist_gb_tuned` — plus an `ensemble` (inverse-sMAPE weights over members that
beat naive on selection folds only).

**Activation gate**: a model is activated ONLY if it beats the naive baseline
on the same folds. Naive winning is a legitimate, common outcome and means
"no exploitable edge at this horizon" — the UI must present it that way, not
hide it.

**Features**: returns and log-returns at multiple lags, rolling means/vols,
RSI, MACD, Bollinger, ATR, Donchian, Keltner, Ichimoku, SuperTrend, PSAR,
pivots, GARCH-lite volatility proxies, day-of-week/Jalali calendar effects,
plus exogenous context for the Iranian symbol: `USD_IRT`, `XAUUSD`, the
parity premium (level, z-score, momentum), and gold-fund price/retail flow.
Global gold is trained WITHOUT Iranian exogenous inputs.

**Self-learning loops** (all fed by matured predictions):
1. **Meta-labeling gate** — a logistic model trained on the system's own
   matured predictions estimates P(this direction call is right) from
   interval width, move size, pre-gate confidence, horizon scale, freshness,
   regime, and a symbol flag; blended 50/50 into confidence. It trains on
   `raw_confidence` (the value before the gate touched it) so its own output
   never feeds back into its features, and refuses to score if the stored
   feature set no longer matches the code.
2. **Adaptive conformal intervals (ACI)** — the nominal 90% interval is
   re-leveled from observed live coverage.
3. **Per-regime live calibration** — directional hit rate and coverage are
   tracked per symbol × horizon × regime (`trending_up`, `trending_down`,
   `ranging`, `high_volatility`); confidence shrinks toward observed reality
   as evidence accumulates.
4. **Live ensemble re-weighting** — member weights adapt to recent live sMAPE
   (bounded to the last 120 days so different market eras are not compared).

**Maturity/labeling**: a prediction matures against the nearest good
observation to its target time within a horizon-dependent tolerance, and the
search window is floored at `predicted_at` — an observation from before the
forecast existed can never be its outcome.

**Reproducibility/tracking**: every run writes `training_runs` (status,
horizons, all candidates evaluated with metrics, selection); every candidate
writes a `model_versions` row (metrics, baseline metrics, params, artifact
path, `is_active`); the winner's artifact is a joblib file under
`MODELS_DIR/<symbol>/<horizon>/`. Runs stranded in `running` by a container
kill are reaped as failed. Superseded artifacts are pruned after 14 days.

## 7. Decision layer (how forecasts become guidance)

- **Round-trip cost** is the live observed dealer spread (from the primary
  provider's buy/sell sides, typically ~0.5%), falling back to a conservative
  1.5% when no recent spread observation exists.
- **Per-horizon tilt**: `favors buying` (projected move > cost AND confidence
  ≥ 55%), `favors selling` (projected drop beyond half the cost AND
  confidence ≥ 55%), `favors waiting` (|move| ≤ cost), `unclear` (clears cost
  but confidence < 55%), `no call` (stale inputs). Each badge shows the exact
  arithmetic on hover.
- **Composite signal** (buy/hold/sell + 0–100 score): weighted blend of
  forecast direction across horizons, confidence, trend/RSI, parity premium
  vs its 30-day norm, and fund flows, with supporting/conflicting factor
  lists, an invalidation condition, and a review time. Signals are computed
  from the Iranian symbol's own predictions only.
- **Monte Carlo odds** on custom horizons: P(up), P(gain clearing costs),
  P(loss beyond costs), and p05/median/p95 outcomes.
- **Backtest engine**: strategy vs buy-and-hold vs SMA-crossover vs no-action,
  gross and net of fees/spread/slippage, with total/annualized return, win
  rate, profit factor, max drawdown, Sharpe-like ratio, directional accuracy,
  and a per-regime breakdown.

## 8. HTTP API (Go, `/api/v1`, JWT bearer unless noted)

Public: `GET /health`, `GET /readiness`, `GET /metrics`, `GET /docs`,
`POST /auth/login`, `POST /auth/register` (closed unless first user or admin).

Authenticated: `GET /auth/me`; `GET /prices/current`, `/prices/history`;
`GET /market/summary` (prices, parity, premium, 30-day average premium,
`trading_cost_pct`, provider health, latest signal), `/market/premium`,
`/market/indicators`, `/market/provider-gap`, `/market/candles`,
`/market/funds`; `GET /predictions[?symbol=]`, `/predictions/custom?days=N`,
`/predictions/{horizon}`; `GET /signals/current`, `/signals/history`;
`GET /models`, `/models/performance?symbol=`; portfolio CRUD +
`/portfolio/import|export`; alerts CRUD + `/alerts/events` + ack;
`POST /issues`.

Admin only: `GET /issues`, `/issues/report`, `/admin/audit`,
`POST /admin/jobs/{job}`, and full user management
(`GET|POST /admin/users`, `PUT|DELETE /admin/users/{id}`).

Errors use `{"error":{"code","message","details"}}`; every response carries
`X-Request-ID`.

## 9. Frontend (11 pages)

Overview (prices, parity premium, advisor, action planner, funds, provider
health), **Brief** (a written narrative: situation → per-horizon expectations
→ Monte Carlo possibilities → one prescription paragraph), Trade panel
(candlesticks with SMA/SuperTrend/pivot overlays), Forecast, Technical,
Drivers, Portfolio (transactions, P/L vs forecasts, CSV import/export),
Alerts, Models (per-horizon metrics vs baseline, live accuracy, registry),
Issues (admin — mirrored WARN/ERROR log), Users (admin).

UI conventions: toman with thousands separators; IRT/IRR and Jalali/Gregorian
toggles; `unicode-bidi: isolate` on numeric spans so RTL currency words do not
scramble adjacent LTR percentages; a permanent banner that predictions are
uncertain estimates, not financial advice; regular users never see
system-scope pages.

## 10. Operations

- **Scheduler** (Go cron, UTC, Redis `SET NX` lock per job so a job never
  double-runs): collect `*/10 * * * *`, predict `5 * * * *`, signals
  `10 * * * *`, evaluate `20 * * * *`, alerts `*/5 * * * *`, train
  `30 2 * * *`, cleanup `0 4 * * *`. Per-job timeouts (training gets 90
  minutes; a full two-symbol run takes ~35–40 minutes).
- **Deployment**: git push from a workstation → `git pull` on the host →
  `docker compose up -d --build`; migrations run automatically at api start.
  Never rebuild the prediction service while training is in flight.
- **Backups**: nightly `pg_dump` (custom format) with 14-day retention and
  optional off-host rsync.
- **Observability**: Prometheus metrics from both services (collection
  successes/failures, last price timestamp, model sMAPE, job last-success,
  prediction duration), plus an in-app Issues log mirroring every WARN/ERROR.
- **Retention**: raw observations 365 days, issue log 30 days, model
  artifacts 14 days past deactivation.

## 11. Security posture

JWT HS256 with an algorithm allowlist and a ≥32-character secret; tokens are
re-validated against live user state on every request (a deleted or demoted
user loses access immediately). bcrypt cost 12. Per-IP rate limiting plus
per-account lockout (5 failures in 15 minutes → 15-minute lock). The reverse
proxy overwrites `X-Forwarded-For` and clears `True-Client-IP` so clients
cannot spoof their identity to the limiter. All SQL is parameterized; every
per-user query is scoped by `user_id`. Secrets live only in the host `.env`
(never committed); Postgres, Redis, the api, and the prediction service are
unreachable from outside the Docker network.

## 12. Testing

~233 Python tests (formula, leakage guards, walk-forward, calibration,
providers with recorded fixtures, market hours, end-to-end pipeline),
8 Go packages (auth, market hours, indicators, prices, alerts, portfolio,
config, HTTP server), ~116 frontend tests (pure advice/brief/format logic and
component behavior), plus type checking and a production build. CI runs all
of it on push.

## 13. Known limitations — state these honestly, never paper over them

- With ~40 folds the holdout is ~12 folds: holdout verdicts are noisy and
  will occasionally reject a good model or admit a lucky one.
- Naive currently wins several short horizons — genuinely no edge there.
- Iranian gold is driven by policy/FX shocks that no price-history model can
  anticipate; interval coverage matters more than point accuracy.
- Single-host, single-replica: in-memory lockout state resets on restart.
- The Tehran-exchange fund feed depends on a third-party mirror with a daily
  quota.

## 14. How to help me

When I ask for a change: identify the affected service(s) and file(s), respect
the invariants above (units, UTC, calendars, point-in-time correctness, the
naive-baseline gate, no look-ahead), and tell me what could break. When I ask
about model quality: distinguish selection-fold from holdout numbers, and call
out leakage, bias, or overfitting risks explicitly. When I ask for trading
guidance: remind me that this system produces uncertain estimates rather than
advice, and reason net of the ~0.5% round-trip cost. If my request conflicts
with something above, say so before writing code.

MY REQUEST:
[write your question here]
