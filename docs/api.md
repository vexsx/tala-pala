# API Documentation

The full machine-readable specification is `backend-go/docs/openapi.yaml`, served at runtime at `GET /api/v1/docs/openapi.yaml` (human landing page at `/api/v1/docs`).

## Conventions
- Base path `/api/v1`; JSON everywhere; UTC ISO-8601 timestamps; Iranian amounts in **toman (IRT)**.
- Auth: `Authorization: Bearer <JWT>` from `POST /api/v1/auth/login`. Health/readiness and auth endpoints are public; everything else requires a token; `/api/v1/admin/*` requires role `admin`.
- Errors: `{"error":{"code":"...","message":"...","details":{}}}` with matching HTTP status. Every response carries `X-Request-ID`.
- Pagination: `page`, `page_size` query params; responses include `total`.
- Rate limits: 60 req/min/IP by default (429 when exceeded), 10/min for login.

## Endpoint summary

| Method & path | Purpose |
|---|---|
| `POST /auth/register` · `POST /auth/login` · `GET /auth/me` | Account & session |
| `GET /health` · `GET /readiness` · `GET /metrics` | Liveness, readiness (DB+Redis), Prometheus |
| `GET /prices/current` | Latest normalized price per symbol + staleness + 24h change |
| `GET /prices/history?symbol&from&to&interval&page` | Historical series (raw/hourly/daily buckets) |
| `GET /market/summary` | Dashboard payload: 18k price, XAU, USD/IRT, theoretical vs observed, premium, provider health, latest signal |
| `GET /market/premium?days` | Theoretical vs observed premium history |
| `GET /market/indicators?days` | SMA/EMA/RSI/MACD/Bollinger/ATR/momentum/ROC/volatility/support/resistance |
| `GET /market/provider-gap?symbol&window_minutes&history_days` | Dispersion between providers quoting the same symbol (current per-provider quotes, gap %, daily gap history) |
| `GET /market/candles?symbol&interval&limit&before&from&to&overlays` | Candles + chart-ready overlays (SMA/Bollinger/Ichimoku/SuperTrend/PSAR) + pivot levels for the Trade panel. `symbol` is a registry code (`KnownSymbols` plus enabled `market_price`/`fx` rows quoted in IRT/USD — ticks from `prices`), `IDX:<insCode>` (a TSETMC index, **close-only**: open/high/low null, `price_fields:["close"]`, `unit:"index_points"`) or `EQ:<insCode>` (a roster share, adjusted, in **rial**; halted sessions `traded:false` with null open/high/low). Tehran symbols are `1d` only (any other interval is the 400 "This timeframe is not available for the current data source.") and add `price_fields`, `unit`, `source`, `instrument`, `data_age`, `notes` and `revision` — CONTRACTS.md Addendum 29 |
| `GET /stocks?enabled&sector` | Tehran equity roster: coverage per symbol plus its corporate-action adjustment verdict (`validated` / `refused` / `never_ingested`). Carries a top-level `data_age` block — see below |
| `GET /stocks/screen?period&numeraire&sector&sort&order&limit` | Screener over the roster: return, volatility, max drawdown and liquidity per instrument from **adjusted** closes, in **rial**, with `excluded` naming every roster symbol that could not be screened and why. Carries a top-level `data_age` block — see below |
| `GET /stocks/{symbol}/bars?adjusted&from&to&limit` | Daily OHLCV in **rial**. `adjusted` defaults to **true**; every response states the `adjustment_version` and how many actions were applied. An adjusted read of a symbol whose adjustment failed validation is `409`, never a quietly-raw series |
| `GET /stocks/{symbol}/relative?period` | The share's adjusted return beside TEDPIX and its own sector index, rebased to 100; beta and correlation over single-session spans. `409` when the share's adjustment is not validated |
| `GET /stocks/{symbol}/flows?period` | حقیقی/حقوقی money flow per session in **toman**, each session checked against two identities; windows of 5/20/60 sessions; cumulative net individual flow |
| `GET /bourse/overview` | Headline indices' newest settled session, TSETMC's live overview at the last fetch (trade value/count, market value, state) and sector breadth. Carries `data_age` |
| `GET /bourse/indices?since&kind&market` | All 71 TSETMC indices with their correction verdict and returns 1w..5y, `since` (the page sends 1 Farvardin), 1-year dispersion, 52-week range, drawdown, distance from the all-time high and SMA200 |
| `GET /bourse/indices/{code}/history?period&from&to&unit` | One index; `unit` = `points` (default), `usd`, `gold` (both rebased to 100) or `real` (monthly, SCI CPI). `409` for an index whose verdict is refused |
| `GET /bourse/breadth?basis&period` | Equal-weighted against cap-weighted (`basis=total`: TEDPIX vs شاخص کل هم‌وزن; `price`: the price indices), rebased, with their ratio |
| `GET /bourse/market-value?period` | Total value of the bourse and the Farabourse per session, in toman and dollars. Size, not a return |
| `GET /bourse/flows` | The roster's money flow over the newest 1/5/20/60 sessions, and the same summed across the roster (not the market) |
| `GET /market/funds` | TSE gold-fund stats: prices, volume, retail buy/sell % (latest + today's averages), buyer power, retail net-flow history |
| `GET /predictions?symbol` · `GET /predictions/{horizon}?symbol` | Latest per horizon · history incl. actuals (symbol: IR_GOLD_18K default, XAUUSD) |
| `GET /predictions/custom?days=N` | On-demand forecast + buy/hold/sell lean for an arbitrary 1–90 day horizon (computed live, not persisted) |
| `GET /issues…` · `GET /issues/report` (admin only) · `POST /issues` (any user) | System issue log + debug digest are admin scope; error reporting stays open to all sessions |
| `GET /signals/current` · `GET /signals/history` | Explainable Buy/Hold/Sell signal |
| `GET /models` · `GET /models/performance` | Model registry · metrics vs baseline + live accuracy |
| `GET/POST /portfolio*` (`/transactions`, `/import`, `/export`) | Holdings CRUD, CSV import/export, valuation, scenarios |
| `GET/POST/PUT/DELETE /alerts*` (`/events`, `/events/{id}/ack`) | Alert rules and in-app events |
| `POST /admin/jobs/{collect\|train\|predict\|signals\|backtest\|evaluate}` | Manual job triggers (proxied to the prediction service) |
| `GET /admin/audit` | Audit log |
| `GET/POST /admin/users` · `PUT/DELETE /admin/users/{id}` | Full user management (list/create/change role/reset password/delete; self-registration is closed) |

## Real returns and the cost of living (`GET /markets/performance`)

### Two deflators, and which one answered

A real return is only as fine as the index behind it, so every row names the
series that deflated it in `real_return_deflator`.

| | `SCI_CPI_URBAN` (preferred) | `WB_CPI_IRN` (fallback) |
|---|---|---|
| publisher | Statistical Centre of Iran | World Bank mirror |
| frequency | **monthly** | annual |
| coverage | 2002-03 → 2026-07 | 1960 → 2025-01 |
| shortest answerable window | ~2 months | 2 calendar years |

The monthly index is tried first and wins wherever it reaches — which is every
asset in `prices`, the earliest of which begins in 2010. The annual series is
used only when a window starts before 2002-03, today just deep Tehran equity
history, and a row that fell back says so in its notes.

**The two are never chained.** A ratio taken across the 2002 boundary would
splice two baskets, two methodologies and two rebasings into a number neither
publisher would endorse. A row uses one deflator or the other.

This matters more than it sounds: under the annual series a **1-year window got
no real return at all**, because deflating needed two covered calendar years.
It was not an approximation — it was a null.

### How long the fall lasted

`max_drawdown_pct` says how far an asset fell. On its own a reader cannot act
on it, because "fell 60%" is a different fact depending on whether the holder
waited three weeks or is still waiting six years later — and on this market the
second is common. Each item now also carries:

| field | meaning |
|---|---|
| `drawdown_peak_date` / `drawdown_trough_date` | the two ends of the worst fall |
| `drawdown_recovered_date` | when the peak was regained — **null when it never was** |
| `drawdown_recovery_days` | trough → recovery: how long the climb back took |
| `underwater_days` | peak → recovery, or peak → today while still under water. The number a holder actually felt |
| `still_underwater` | whether the peak has yet to be regained |
| `current_drawdown_pct` | the last close against the highest close before it; `0` means a new high |

Two things these numbers are not, both stated in the item's `notes` rather than
left to be assumed:

- **Days are CALENDAR days, not trading sessions.** The Tehran exchange is shut
  two days in seven plus Nowruz, so a session count understates the wait by
  about a third — and the question is about the holder's life, not the
  exchange's diary.
- **The prices are NOMINAL.** Regaining a toman level after years of Iranian
  inflation is still a large real loss. A real-terms drawdown is **deliberately
  not offered**: the deflator is monthly, and holding its level flat across a
  month would make the price level jump at every reference-period boundary, so
  a month with 5% inflation would appear as a 5% one-day fall. A drawdown table
  whose troughs are partly artifacts of the deflator's frequency is worse than
  no table, and interpolating the index onto days would invent an intra-month
  path nobody measured.

The worst drawdown is the worst **fall**, not the lowest close: a series that
drops 50% from an early peak, then makes a higher peak and eases 10%, has its
worst drawdown at the first trough even though the later dip's close is
numerically greater.

### Tehran equities in `items`

Equities are ordinary rows, measured by the same code, in the same unit, over
the same window as gold and the dollar. `domain` is `ir_equity` and `code` is
the Persian trading symbol, the same one `/stocks/{symbol}/bars` resolves.

| property | value | why |
|---|---|---|
| `quote_currency` | `IRT` | TSETMC quotes **rials**; the closes are divided into toman once, at the loader. Returns are ratios so the ten cancels — a wrong unit would hide in every percentage and surface only in an end-of-window level printed beside gold |
| `is_derived` | `true` | the close served is the raw close times a cumulative corporate-action factor this platform computed, so it will not match a TSETMC screen |
| `is_proxy` | `false` | these are the exchange's own prints |

**Only validated adjustments appear.** An instrument whose corporate-action
adjustment the gate did not pass is excluded and named in `warnings`, with one
of four distinct reasons: disabled, no stored bar, never adjusted, or refused
(carrying the gate's own refusal text). It is never served raw — فولاد is
×1.52 raw against ×907.86 adjusted over nineteen years, because capital
increases are not price moves.

Halted sessions are dropped: TSETMC stores a halt as a bar whose `final_close`
is the carried reference price rather than a print.

Cross-checked on production: all 19 symbols' returns agree with
`/stocks/screen` to **0.0000 percentage points**, which is two independent code
paths validating the adjustment, the halt filter and the windowing at once.

### `cost_of_living`

A **separate array** from `items`, holding what parts of the consumer basket
cost over the same window. Separate because these are price indices nobody can
buy: they must not be sortable with the assets or eligible to be the best
performer.

| field | meaning |
|---|---|
| `growth_pct` | the index's own change over the covered window |
| `real_growth_pct` | that change against the **headline** index over the *same* reference periods. Positive = this part of life outpaced the basket; negative = it got relatively cheaper |
| `periods` | reference months actually spanned, which can be fewer than requested |

Because both legs come from one publisher with identical periods, this is the
one real comparison in the API with **no leg mismatch to disclose** — unlike the
asset rows, which must anchor a daily series inside a monthly period and report
the residual in days.

Two things this section deliberately cannot say:

- **Shelter is not house prices.** `SCI_CPI_HOUSING` and `SCI_CPI_RENT`
  correlate at 0.999996 across all 293 months and never diverge more than 1.11%
  — the Iranian housing division is rent-dominated, so they are one fact twice.
  Shelter is published **once**. There is no house price index on this
  deployment, so the API cannot say what a home was worth, only what shelter
  cost.
- **An index has no numéraire.** Gold in dollars is a price; the vehicle index
  in dollars is nothing, because the index is already a ratio to its own base.
  These rows are never converted, whatever `?numeraire=` asked for.

## Equity data age (`data_age`)

`GET /stocks` and `GET /stocks/screen` both carry a top-level `data_age` block.
Tehran equity bars are a **manually refreshed** dataset — `cdn.tsetmc.com` is
unreachable from the production host, so bars arrive only when an operator runs
`make refresh-equities` from a network that can reach it (docs/deployment.md).

```json
"data_age": {
  "newest_trade_date": "2026-09-09",
  "age_days": 15,
  "as_of": "2026-09-24",
  "stale": true,
  "stale_after_days": 10,
  "refresh_command": "make refresh-equities",
  "warning": "These prices are 15 days old: the newest stored Tehran session is 2026-09-09 and today is 2026-09-24. …",
  "note": "Tehran equity bars do not refresh themselves: …"
}
```

* `age_days` is measured against **today**, not against the response's `to`
  bound: it describes the dataset, so a caller asking for a historical window
  still learns where the underlying data stops.
* `newest_trade_date` and `age_days` are `null` — never `0` — when the roster
  holds no bars at all. A zero would read as "collected today".
* `stale` is `age_days > stale_after_days`, exposed as a flag so a client
  renders a banner without reimplementing the bound and drifting from it. The
  same 10-day bound drives the `EquityBarsStale` alert.
* `warning` is present whenever these prices must not be read as current, and
  names the command that fixes it. On the screener it is also appended to
  `warnings`, so a client already rendering that list needs no change.

The age is on the payload rather than left for the client to derive from
`last_trade_date`, because a client that never does the subtraction is exactly
the one that renders a fortnight-old price under today's heading — which is
what this endpoint did for fifteen days in September 2026.

## The Tehran market (`/bourse/*`, migration 0029)

Everything here arrives in the same off-server fetch as the equity bars
(`make refresh-equities`), so the `data_age` block has the same shape and the
same 10-day bound; it follows TEDPIX's newest session. Five rules, each from a
measurement on production data:

* **A power of ten is corrected, nothing else is.** Six indices carry values
  TSETMC stores off by exactly a factor of ten (26 steps; the second-market
  index has been stored /10 since 2026-08-16). The raw close is kept and a
  per-row `scale_exp` corrects it, anchored on the majority scale and checked
  against the exchange's live figure on every ingest. Every index carries its
  `check` (`scale_breaks`, `rows_rescaled`, `live_ratio`, `largest_move_pct`),
  and one whose check is `refused` serves no corrected value.
* **A repeated value is a closed market.** TEDPIX stood at 3,713,955.9 for 50
  sessions from 2026-02-25. Those rows are served (`unchanged: true`) and drawn
  flat, and left out of every dispersion figure — counting them would report
  the year's session dispersion as 1.314% instead of the 1.455% it traded at.
* **Windows end at the newest stored session**, and a return is measured from
  the value in force when the window opened, so "since 1 Farvardin" is from the
  last close of the previous Jalali year.
* **Beta uses single-session spans only.** A share's return from one traded
  session to the next is paired with the index over the same dates, and a span
  across a halt is excluded and counted (`multi_session_spans`): one 5.5-month
  span once put فولاد's correlation with TEDPIX at −0.09; without it, 0.77.
* **Market value is the publisher's latest statement.** TSETMC restates this
  aggregate — two fetches an hour apart differed on 18 bourse sessions — so it
  is the one series here stored as last-write-wins, with every restatement
  counted in the ingest report. Bars, index closes and money flow came back
  identical and are never overwritten.

Money flow: a session counts only when buying equals selling and the total
equals the traded value on that session's daily bar; one failing either is
returned with `consistent: false` and its `excluded_reason`, and left out of
every sum. None of these figures is a forecast.

## Example

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"..."}' | jq -r .token)

curl -s http://localhost:8088/api/v1/market/summary -H "Authorization: Bearer $TOKEN" | jq
```

The internal Python API (`/internal/*`, port 8500) is documented in `docs/CONTRACTS.md`; it is not reachable from outside the Docker network and requires the `X-Internal-Token` header.
