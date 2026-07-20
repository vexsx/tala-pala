# Iranian Gold Formula, Rial/Toman Units, and the Local Premium

## Units — read this first

- **1 toman (IRT) = 10 Iranian rials (IRR).** Iranian market sites (including TGJU) quote most prices in **rials**, while people quote prices to each other in **tomans**.
- This system stores and exchanges every Iranian value in **toman (IRT)** internally. Provider adapters convert at ingestion (rial ÷ 10) and keep the original rial value in `raw_observations` with its original unit, so every conversion is auditable.
- Every stored observation carries `currency` and `unit` columns. Values with different currencies are never mixed in arithmetic; the frontend offers a display-only IRT/IRR toggle (×10).
- Timestamps are stored in UTC; the UI renders Asia/Tehran time and both Gregorian and Persian (Jalali) calendars.

## Theoretical price of 18k gold

One troy ounce = **31.1034768 grams**. 18-karat gold is 18/24 = **0.750** pure.

```
pure_gram_usd  = XAUUSD / 31.1034768          # USD per gram of pure gold
pure_gram_irt  = pure_gram_usd × USD_IRT      # toman per gram of pure gold
theoretical_18k_irt = pure_gram_irt × 0.750   # toman per gram of 18k gold
```

Worked example (illustrative numbers): XAUUSD = 3,300 USD/ozt, USD_IRT = 100,000 toman/USD →
pure gram = 106.0975 USD → 10,609,748 IRT → **18k theoretical ≈ 7,957,311 IRT/gram**.

Implemented in `prediction-python/app/core/formula.py` with exact-value unit tests.

## Why the observed Iranian price differs from the theoretical price

```
premium_pct = (observed_18k − theoretical_18k) / theoretical_18k × 100
```

The observed TGJU/market price routinely deviates from the formula because of:

- **Local demand and supply** — gold is a primary inflation hedge in Iran; demand surges move the local price independently of the ounce.
- **Currency-market fragmentation** — the free-market USD rate itself carries a spread and regional variation; the official CBI rate is far lower and is *not* used for this calculation.
- **Trading spread and dealer margin** — retail buy/sell spreads of roughly 1–2% are typical.
- **Fees and taxes** on manufactured gold vs raw bullion.
- **Market-hours mismatch** — the global ounce trades nearly 24/5; the Tehran market has its own hours and holidays, so gaps open at opens/closes and over weekends (Iranian weekend is Thu/Fri vs global Sat/Sun).
- **Political/economic risk premium** — sanctions or geopolitical news often hit the USD rate and gold demand simultaneously.
- **Data delays** from source publication cadence.

The system therefore treats the theoretical price as a **benchmark and model feature**, not as the prediction itself. It computes and displays: theoretical 18k, observed 18k, absolute difference, premium %, the 30-day average premium, and raises an alert when the premium's z-score against its recent history exceeds a configurable threshold (default 2.0) — an abnormal premium historically tends to mean either a currency move is being priced in or the local market is overheated.

## Coin bubble (context)

Iranian coins (Emami, etc.) trade with their own premium ("bubble") over melt value. The Emami coin price is collected as `IR_COIN_EMAMI` and its implied bubble is available as a market-driver feature; it often leads retail sentiment.

## Validation

- `test_formula.py` checks the constants and worked examples to 6 decimal places.
- `test_normalize.py` checks rial→toman and the Yahoo `^TNX` (yield ×10) normalization.
- Collection-time validation rejects values that imply an impossible premium (e.g. |premium| > 25%) as `suspect` unless confirmed by a second source — this catches unit mistakes (rial/toman mixups produce ×10 errors that this check catches immediately).
