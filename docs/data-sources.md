# Data Sources

**Access date for all sources: 2026-07-20** (verified live on that date). Every stored observation records its provider, raw value, unit, currency, and collection time; provider priority/enable flags live in the `data_providers` table.

## Priority policy

1. Official or documented API → 2. licensed/reliable public API → 3. structured public data → 4. careful HTML parsing only when permitted. No source is bypassed past authentication, CAPTCHA, or anti-bot measures — if a source blocks automated access, we drop to the next provider instead.

## Iranian gold & FX

### TGJU — unofficial JSON endpoints (live fallback; daily-close source for eight series)
> History of the live feed: `call2`/`call3`/`call4.tgju.org` answered scripted clients with "access denied" from 2026-08-02 (2047 consecutive failures), so migration 0023 disabled the provider on 2026-08-13. Re-tested from the production host on 2026-09-08, all three hosts and the history endpoint served again, and migration 0025 re-enabled it at priority **22** — strictly a live **fallback**: 18k → Hamrah Gold then Milli Gold, USD → BitMax, XAU → Yahoo, Emami coin → alanchand/BrsApi stay primary.

- **Live snapshot**: `https://call2.tgju.org/ajax.json` (fallbacks: `call3`, `call4`). One request returns ~830 indicators under `current`, each `{p, h, l, d, dp, dt, ts}` with comma-formatted **rial** strings. Live collection reads only `geram18` (18k gram), `sekee` (Emami coin), `price_dollar_rl` (free-market USD) and `ons` (global ounce, USD) — `providers/tgju.py` `SLUG_MAP`.
- **Daily history**: `https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}` — DataTables JSON `{recordsTotal, recordsFiltered, data}`, rows newest first, `[open, low, high, close, change(HTML), change%(HTML), gregorian_date, jalali_date]`, every number a comma-formatted rial string. Measured 2026-09-29, from this Mac and from the production host (~2 s per full table there, honest User-Agent): no paging parameter returns every row, `length=N` the N newest, a large `length` (the jobs send 20,000) the whole table — and **`length=-1` silently drops the oldest row**. The day in progress is never in the table. The `silver` slug (the global ounce in USD) hangs and is not used.
- **Server-side daily closes (`tgju_daily`, migration 0031)**: `POST /internal/tgju/daily`, scheduled by the Go API (`SCHEDULE_TGJU_DAILY_CRON`, default `25 0,12 * * *` UTC). It reads the whole table on a symbol's first pass (checked against `recordsTotal`), the 40 newest rows after that, and stores **CLOSE ÷ 10 as toman** with `source = 'tgju_history'`, stamped 23:00 UTC on the bar's own date, only for days strictly before the current Tehran date, each with a `raw_observations` row holding TGJU's rial number and the printed cell. A close more than ×1.6 from the median of its seven neighbours either side is held as `suspect` in `raw_observations` and never stored; a refetched close that differs from a stored one is counted as a restatement and left alone. These are **one settled close per day — not live quotes**, and they are deliberately not in live collection; `/prices/current` marks them `cadence: "daily_close"` and calls one stale only when it is more than 4 calendar days old. With `data_providers.enabled = FALSE` for `tgju` the job skips every symbol with that reason (not a failure) and makes no request.

  | Registry code | Slug | Unit (stored) | TGJU history (rows, first day) | Note |
  |---|---|---|---|---|
  | `IR_SILVER_999` | `silver_999` | toman / gram | 1,674 from 2020-10-31 | tier `commercial`: TGJU-only; junk closes 8,000 (2021-08-10), 8,200 (2021-08-29) and 2,008,800 rial (2022-09-30) are held as suspect; median premium +4.3% over XAGUSD × USD_IRT / 31.1035 |
  | `IR_COIN_BAHAR` | `sekeb` | toman / coin | 3,496 from 2013-07-22 | per coin, premium over gold content included |
  | `IR_COIN_HALF` | `nim` | toman / coin | 3,438 from 2013-07-22 | per coin |
  | `IR_COIN_QUARTER` | `rob` | toman / coin | 3,423 from 2013-07-22 | per coin |
  | `IR_COIN_GERAMI` | `gerami` | toman / coin | 3,360 from 2013-07-22 | per coin |
  | `IR_GOLD_24K` | `geram24` | toman / gram | 3,374 from 2014-05-02 | **derived by TGJU**: geram18 × 4/3 (median ratio 1.33332 over 2,963 days) — `derived_from = IR_GOLD_18K`, so its return in grams of 18k gold is withheld |
  | `IR_GOLD_MESGHAL` | `mesghal` | toman / mesghal (4.6083 g at 705‰) | 3,517 from 2013-07-22 | **derived by TGJU**: geram18 × 4.3318 (median 4.3316) — `derived_from = IR_GOLD_18K` |
  | `IR_COIN_EMAMI` (gap-fill only) | `sekee` | toman / coin | 4,301 from 2010-04-04 | written **only on UTC days with no observation from any source, from 2026-04-27 on**, judged once the UTC day has ended (production's 83-day hole 2026-04-28 → 2026-07-20 begins after the deep backfill's last rows; an empty day before it is a Friday or holiday of the backfilled era and is not filled); the instrument note says so. `USD_IRT` (the USDT/toman market, a different instrument from TGJU's cash dollar) and `IR_GOLD_18K` are never filled |

  TGJU's closes agreed with production's end-of-day tgju/BrsApi values for the Emami coin on 29 of 30 days (worst 0.27%, 2026-07-19 → 09-28).
- **Deep backfill** (`POST /internal/backfill/tgju-history`, manual): the same endpoint splices TGJU history in front of the live era for `IR_GOLD_18K`, `USD_IRT` and `IR_COIN_EMAMI`, and records the splice (see `jobs/tgju_backfill.py`).
- **Unit**: **RIAL** — triple-confirmed (page labels "ریال"; parity arithmetic closes in rials within +0.26% of theoretical on the access date; alanchand's IRR figure matches). Every path divides by 10 → toman through one function (`normalize_history_value`) and keeps the raw rial value.
- **Licensing/ToS** (unchanged): TGJU has **no free documented API**; it sells an official paid feed (https://www.tgju.org/form/api). The endpoints above are what its own front-end uses (CORS `*`), openly reachable, but unofficial — treated as tolerated-but-unlicensed. We access them with an honest User-Agent, courtesy delays, bounded retries and backoff: ≤1 live request per collection cycle, and eight history requests twice a day (one per series). For commercial redistribution, buy the official feed.

### BrsApi (fallback; free key)
`https://Api.BrsApi.ir/Market/Gold_Currency.php?key=...` — free tier 1,500 req/day; 18k/24k/melted gold, ounce, all coins, currencies. **Quotes in toman** (documented `"unit": "تومان"`). Enabled when `BRSAPI_KEY` is configured.

### Navasan (optional; keyed)
`https://api.navasan.tech/latest/?api_key=...` — free tier 120 calls/month (2h update cadence); paid tiers for real-time. Unique value: pre-computed bubble/premium symbols (`bub_18ayar`, `bub_sekkeh`, …). Unit is nominally IRR but **must be verified per symbol with a live key** before trusting — the adapter cross-checks magnitude against TGJU and marks mismatches `suspect`.

### Alanchand (fallback; two modes)
API mode: `https://api.alanchand.com?type=gold&symbols=18ayar,...` — Bearer token, 65 USDT/6mo (`ALANCHAND_TOKEN`). Keyless HTML mode (verified 2026-07-20): `https://alanchand.com/en/gold-price/18ayar` server-renders the 18k price in **rial** in plain HTML — parsed defensively as a fallback for `IR_GOLD_18K` only. No protections are circumvented; if the page ever adds them, the provider fails gracefully.

### Milli Gold (PRIMARY for 18k since 2026-07-21; keyless HTML)
`https://milli.gold/` (verified 2026-07-20) server-renders "قیمت ۱ گرم طلای ۱۸ عیار" in **rial** (Persian digits handled). `IR_GOLD_18K` only, priority 5 (migration 0011). Chosen as primary because it is a 24-hour online trading platform — its quote updates around the clock on Iranian trading days, where TGJU's bazaar ticker stops evenings. Iranian off-days (Thursday + Friday, Tehran) still apply as market closure. Note the quote reflects retail platform pricing (includes their margin); the cross-provider gap panel tracks its spread vs TGJU. TGJU remains the 18k fallback and the source for USD_IRT, the Emami coin, and history backfill.

### Evaluated and not used
- **Bonbast** — paid only ($450+/yr), license forbids competing use, and its public page loads values through deliberately obfuscated rotating-token requests — an anti-scraping measure we do not bypass (unlike plain server-rendered pages, which we do parse). Its daily archive mirror (github.com/SamadiPour/rial-exchange-rates-archive, MIT) remains a legitimate free backfill source.
- **priceto.day** — free but behind Cloudflare JS challenges + rate limiting (verified blocked server-side); we do not bypass anti-bot systems. Its upstream dataset (github.com/margani/pricedb, MIT) is usable directly for backfill.
- **Hamrah Gold** — **no official public API** (site/app/terms document none; API subdomains 404). Per project policy, no scraping of the PWA's private endpoints; holdings are entered manually or by CSV.

## Global market data

| Source | Use | Notes |
|---|---|---|
| TGJU `ons` | ~~Primary XAU/USD~~ — fallback only | Live sources took precedence in 2026-07 (its ticker lagged the market by 30–60 min); the provider was disabled 2026-08-13 (0023) and re-enabled 2026-09-08 at priority 22 (0025), behind Yahoo for XAU. Its spot ounce is never spliced into the `GC=F` futures series |
| Yahoo Finance chart API (`GC=F`, `SI=F`, `BZ=F`, `DX-Y.NYB`, `^TNX`) | Secondary global (gold futures proxy, silver, Brent, DXY, US10Y) | Unofficial, personal-use scale only; ToS forbid redistribution. `^TNX` is served under **two** conventions and Yahoo switches between them in **both** directions — the plain yield (`4.697` = 4.697%) up to 2026-08-11, the CBOE index (yield×10, `46.82` = 4.682%) from 2026-08-11, and the plain yield again by 2026-08-13 (`regularMarketPrice` 4.627, fetched live). So the convention is read off the quote against the 25% plausibility ceiling and never assumed, and a *history* payload is settled once for the whole series from its maximum rather than bar by bar; see Addendum 25 |
| metals.dev / goldapi.io / metalpriceapi.com | Optional keyed cross-check | Free tiers ~100–500 req/mo; enabled via API keys |
| Stooq CSV | Historical backfill fallback | Now behind a JS anti-bot challenge for live scraping (verified); adapter kept for when CSV access works, never bypassed |
| FRED | **Not usable for gold** | LBMA gold series removed 2022-01-31 |

## USD/IRR: official vs free market
Iran runs a multi-tier FX system. The **CBI/ETS official rate** (≈1.33M IRR/USD, June 2026) applies to managed imports and is ~40% below the **free-market rate** (≈1.88M IRR ≈ 188,100 toman on access date) that actually prices street gold. This system uses the free-market rate (`USD_IRT`) for all parity math; the official rate is deliberately excluded.

## Premium context (for validation thresholds)
The 18k local premium vs theoretical parity is normally within **±3%** (range roughly −5%…+10%; +0.26% on access date), spiking during panic-buying and going negative when USD outruns gold demand. The Emami **coin** bubble is structurally larger (often 10–40%). Collection-time validation flags any 18k observation implying |premium| > 25% as `suspect` (this catches rial/toman ×10 mistakes instantly), and the UI alerts when the premium z-score exceeds 2.0.

## Adding or replacing a provider
1. Implement a class in `prediction-python/app/providers/` (subclass `BaseProvider`, return `Observation`s with explicit raw unit/currency).
2. Insert/enable a row in `data_providers` with a priority (lower = tried first).
3. Add a fixture test in `prediction-python/tests/` with a saved real response.
No Go/frontend changes needed — everything downstream reads normalized `prices`.

## Hamrah Gold (pwa.hamrahgold.com) — PRIMARY 18k source (2026-07-23)

Public pre-login price ticker of the Hamrah Gold 24/7 online trading platform:
`GET /api/v1/market/price/xau/changes?type=sell|buy` (unauthenticated JSON,
rial per 18k gram). The provider emits the buy/sell midpoint; both sides and
the spread stay in `raw_payload`. Fetched with the project's honest
User-Agent at the normal collect cadence. Consistent with the repo ethics:
public data only, no accounts, no private Hamrah Gold data (the portfolio
deliberately never connects to Hamrah accounts).

## TSETMC market data (off-server, since migration 0029)

Fetched by `scripts/tsetmc_fetch.py` in the same run as the equity bars, from
a network that can reach `cdn.tsetmc.com` (the production host cannot), and
ingested through `POST /internal/bourse/ingest`. The endpoints were found in
TSETMC's own public web client and each was measured before use:

| Endpoint | What it gives | Measured |
|---|---|---|
| `Index/GetIndexB2History/{insCode}` | Daily close, low, high of one index (no open) | 71 indices, 256,419 rows. low/high are not a band (409 TEDPIX rows close outside them); 26 x10 steps across six indices; 11 zero rows in four dormant sectors |
| `Index/GetIndexB1LastAll/All/{1,2}` | Every index's live value and % change | Used only to check the corrected history; after correction all 71 agree |
| `MarketData/GetMarketValueByFlow/{1,2}/9999` | Total market value per session, rials, from 2019-12-24 | Restated between fetches (largest −1.73%); stored as the latest statement |
| `ClientType/GetClientTypeHistory/{insCode}` | Individual/institutional buy/sell counts, volumes, values per session | Total equals the bar's traded value exactly on 3,835 of 3,848 فولاد sessions |
| `MarketData/GetMarketOverview/{1,2}` | Live trade value, count, volume, market value, state (`S` open, `P` closed) | A snapshot at fetch time, labelled as such |
| `MarketData/GetSectorsSummary` | Per sector: instruments down >2%, down <2%, up <2%, up >2% | TSETMC's own buckets; counts grow through the session (1,725 at 11:50, 2,974 after the close on 2026-09-29) |

The CDN answers intermittently with HTTP 502 (the same URL: 200, 200, 200, 502,
200 two seconds apart), which curl 8.7 reports as exit 56; the script retries
with `--retry-all-errors`. Only settled sessions are stored: a row dated on the
fetch's own Tehran day is kept only when the fetch ran after 15:00.

### The whole market's money flow and the commodity funds (since migration 0030)

The same run now fetches every listed share, not only the roster, and the
commodity funds' closes. Ingested through `POST /internal/bourse/shares/ingest`
(a manifest, in `universe` / `flows` / `sessions` parts, the last two in
offset/limit chunks) and `POST /internal/bourse/funds/ingest`. Measured
2026-09-29:

| Endpoint | What it gives | Measured |
|---|---|---|
| `ClosingPrice/GetMarketWatch?…&showTraded=false` | Every instrument: `insCode` (string), `insID`, symbol `lva`, name `lvc`, sector `csv` | 3,786 rows, 2.25 MB (333 KB gzipped). Shares are picked by `insID` prefix — IRO1 bourse 648, IRO3 Farabourse 354, IRO7 پایه 150, IRO5 نوآفرین 10 (checked with GetInstrumentInfo: companies) — never by `yVal`/`flow`/`cGrValCot`, which were absent from five responses in a row. `insID[8:]` is the board (0001 main, 0002 block, 0003 secondary); `insID[:8]` the company; `insID` is not the ISIN. `csv` has a trailing space, and on options/bonds/funds it is the underlying's or issuer's sector — which is why they are excluded. |
| `StaticData/GetStaticData` | TSETMC's industrial-group names (`type: IndustrialGroup`, integer `code`) | 66 groups covering 52 of the 53 `csv` codes; 84 (one IRO5 share) has no name anywhere, so none is invented. |
| `ClientType/GetClientTypeHistory/{insCode}` | Every session of one share's حقیقی/حقوقی flow, whole history, newest first | The only per-share history TSETMC has: there is no market-wide client-type endpoint by date (`GetClientTypeAll/{date}` 404s; `GetClientTypeAll` is today's volumes only, no values). The single-date form `…/{insCode}/{dEven}` answers HTTP 500 with an empty body for a date without a session, so it is not the incremental path. The whole history is fetched (`--compressed`: فولاد 260 KB instead of 1.32 MB) and trimmed before shipping to what the server lacks plus 10 overlap rows; a share outside the roster ships nothing before 2025-03-21. |
| `ClosingPrice/GetInstrmentsHistoryInDay/{yyyymmdd}` | One whole session: every TRADED instrument's `pClosing`, `pDrCotVal`, `priceYesterday`, `qTotCap`, `qTotTran5J`, `zTotTran` | 2,290 rows (919 KB, 180 KB gzipped) for 2026-09-28; a Friday gives an empty list. `dEven` is 0 on every row, so the date is the requested one and the ingest cross-checks it against the stored flows. `insCode` is a bare JSON number and 2,012 of 2,290 exceed 2^53 — never parse it through a double. Its value equals the flow history's buy total exactly (8 of 8 instruments compared), and its `priceYesterday` is TSETMC's corporate-action-ADJUSTED reference (فولاد: 3,982 on the 2025-03-12 ex-date against a previous close of 5,530; 4,800 on 2024-07-22 against 5,200). Fetched for every session with ≥200 shares' flow rows since 2025-03-21 that is not stored, plus the newest two that are, plus any stored day that shares joined the universe after (it could only store the shares known then; `GET /internal/bourse/shares/state` names those days as `session_dates_incomplete`). |
| `ClosingPrice/GetClosingPriceDailyList/{insCode}/0` (funds) | A commodity fund's whole settled history | عیار, طلا, کهربا (TSETMC sub-sector 6822, gold-based), سیلور, سیمین (6823, silver-based). All list at 10,000 rials; over 5,846 session pairs of one CDN copy the reference equals the previous close except twice, by one rial — no splits (the other copy lacks 2023-03-27, see below). 190 zero-trade rows are carry-forwards and are not stored. Stored in `prices` as toman (÷10), source `tsetmc_cdn`, stamped 23:00 UTC on the session date (funds trade until 18:00 Tehran), and only for sessions before the first day a live source (BrsApi's `tse_funds`, from 2026-07-21 for عیار and طلا) observed the fund: a settled close beside the live last trade on the same day would be two different numbers for one day's price. سافرون is NOT ingested: TSETMC classes it only as an agricultural commodity fund (6824), never as saffron. |

A full refresh is ~1,500 requests (~1,160 histories, ~305 day files the first
time, the rest a few dozen), started 0.5 s apart (`--delay`; never closer than
a third of a second): expect **about 20 minutes for the first run and about 12
for a weekly one**. A failed download costs its share or its date, not the run;
the script prints each failure (every per-item error of every ingest call,
the bars, the market and the sector names included), each contradiction, the
delisted shares and the timings, and exits 1 if anything failed. The run's
copy inside the prediction container is removed once the ingest calls are
done; the archive under `backups/tsetmc/<run>` is kept. `--no-shares` restores
the roster-only money flow, `--flows-since` moves the floor for non-roster
shares and day files, and `--dry-run` reads the server's state, downloads only
the small payloads (the market-level files and the market watch) and prints
what the bulk loop would fetch, without fetching it.

**TSETMC serves two copies of its history.** Fetched five minutes apart on
2026-09-29 (three runs), the CDN answered two versions of the same data: one
printed every index to six significant digits and the other to one decimal
(64 of 71 index histories differ on 8–437 dates, all within a relative 1e-5
or a tenth of a point); one lacked the whole 2023-03-27 session in every
daily list; four roster bars (2021-12-15, 2022-03-26), Tala's 2021-12-15
close (ten rials) and two rows of the 2026-09-28 day file differed by a
trade or two; three indices differed by 0.12–0.72% on one date each. So:
index values are compared to within the two print formats; a stored row the
current copy states differently is **kept** (never overwritten) and listed
under RESTATED at the end of the run, which does not fail it; new rows are
stored beside it; and only a payload unlike MOST of what it overlaps (at
least four rows compared) — over the whole overlap or over its newest ten
rows — fails its item, as does a new row that joins onto a stored row the
payload restates (it would meet the stored one with a step, or for a bar a
corporate action, that nobody made; a fund's new close is judged against the
stored close it follows). A fund list that skips a session TEDPIX records is
a gap in that copy, not a restated reference. A corporate action detected
across the missing session is retired as soon as the other copy stores it,
because the actions are derived over the stored bars. Every other copy
served of a stored bar is kept (`equity_bar_alternatives`, 0032): one copy
disagrees with itself (کگل's 2021-12-15 close is 20,620 there, the next
reference 20,610 in both copies), and a break in the reference chain that
some served copy chains is contested, not an action — so which copy answered
first no longer decides whether the adjusted history carries it.

