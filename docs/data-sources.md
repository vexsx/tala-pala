# Data Sources

**Access date for all sources: 2026-07-20** (verified live on that date). Every stored observation records its provider, raw value, unit, currency, and collection time; provider priority/enable flags live in the `data_providers` table.

## Priority policy

1. Official or documented API → 2. licensed/reliable public API → 3. structured public data → 4. careful HTML parsing only when permitted. No source is bypassed past authentication, CAPTCHA, or anti-bot measures — if a source blocks automated access, we drop to the next provider instead.

## Iranian gold & FX

### TGJU (primary) — unofficial JSON endpoints
- **Live snapshot**: `https://call2.tgju.org/ajax.json` (fallbacks: `call3`, `call4`). One request returns ~830 indicators under `current`, each `{p, h, l, d, dp, dt, ts}` with comma-formatted **rial** strings. Symbols used: `geram18` (18k gram), `geram24`, `ons` (global ounce, USD), `price_dollar_rl` (free-market USD), `sekee` (Emami coin), `mesghal`.
- **Daily history**: `https://api.tgju.org/v1/market/indicator/summary-table-data/geram18` — DataTables JSON, rows `[open, low, high, close, change(HTML), change%(HTML), gregorian_date, jalali_date]`, ~3,458 records (full history). Used by the seeding script; change fields require HTML-tag stripping.
- **Unit**: **RIAL** — triple-confirmed (page labels "ریال"; parity arithmetic closes in rials within +0.26% of theoretical on the access date; alanchand's IRR figure matches). Our adapter divides by 10 → toman and keeps the raw rial value.
- **Licensing/ToS**: TGJU has **no free documented API**; it sells an official paid feed (https://www.tgju.org/form/api). The endpoints above are what its own front-end uses (CORS `*`), openly reachable, but unofficial — treated as tolerated-but-unlicensed. We access them with an honest User-Agent, ≤1 request per collection cycle (default every 10 min), caching, and backoff. For commercial redistribution, buy the official feed.

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
| TGJU `ons` | **Primary XAU/USD** | Same feed as Iranian data → timestamps coherent for premium math |
| Yahoo Finance chart API (`GC=F`, `SI=F`, `BZ=F`, `DX-Y.NYB`, `^TNX`) | Secondary global (gold futures proxy, silver, Brent, DXY, US10Y) | Unofficial, personal-use scale only; ToS forbid redistribution. `^TNX` quotes yield×10 — normalized ÷10 |
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
