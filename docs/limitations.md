# Known Limitations & Financial-Risk Disclaimer

## ⚠️ Financial-risk disclaimer

**This application is a decision-support and analytics tool. It is NOT financial advice, and its forecasts are uncertain estimates.** Gold and currency markets — especially the Iranian free market — move on political news, sanctions developments, and central-bank actions that no statistical model can anticipate. Backtested or historical performance does not guarantee future performance. You alone are responsible for your investment decisions. Never invest money you cannot afford to lose, and consider consulting a licensed financial advisor.

The application enforces this stance in its own language: signals say "conditions currently favor…" and always ship with confidence, risks, and invalidation conditions; stale data forces a `hold` signal; and models that can't beat a naive baseline are never presented as better than naive.

## Data limitations
- **No official TGJU API.** Iranian price data comes from unofficial JSON endpoints and fallback providers; any of them can change or disappear. The provider registry, health checks, and fallbacks mitigate but cannot eliminate this. (See docs/data-sources.md.)
- **Free-market USD/IRT is fragmented** — different sources quote slightly different rates; the recorded source is attached to every observation.
- **History depth**: intraday history accumulates only from first deployment; intraday horizons (1h/4h) stay disabled until ≥14 days of hourly coverage exists. Bundled sample CSVs are clearly synthetic (for development/tests) — real deployments should seed from live/backfill sources.
- **No trading volume** for the Iranian OTC gold market; volume-based features are unavailable.

## Model limitations
- Forecast horizons beyond a few days on a politically-driven series have wide intervals; the 30d horizon is directional context, not a precise target.
- Prediction intervals are calibrated on historical residuals; genuine tail events (currency devaluations, sanctions shocks) will exceed them.
- Directional accuracy near 50–60% is realistic for daily gold; treat signals as tilted odds, not certainties.
- Models retrain daily; between retrains, a regime shift can degrade live accuracy — the degradation alert exists for exactly this.

## System limitations
- Single-node deployment; Postgres is not HA. Take occasional `pg_dump`s (one-liner in docs/deployment.md) if the portfolio history matters to you.
- Karat conversion in the portfolio (k/18 scaling) is a linear approximation of exchange value; real dealer quotes for 21k/22k/24k include their own margins.
- Alerts are in-app only for now. The alert evaluator writes `alert_events`; adding Telegram/email/webhook is a documented extension point (`backend-go/internal/alerts/` — implement the `Notifier` interface and register it).
- TimescaleDB is not used; if data volume grows to many millions of rows (e.g., minute-level collection for years), migrate `prices` to a hypertable — the schema is compatible.
- The Jalali calendar handling covers display and calendar features; official Iranian market holiday calendars are approximated (weekends Thu/Fri) rather than sourced from an authority.

## Replacing a data provider
See docs/data-sources.md § "Adding or replacing a provider": implement the provider class in `prediction-python/app/providers/`, register it in the `data_providers` table with a priority, and add a fixture test. No other service changes needed.
