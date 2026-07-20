# Iran Gold Predictor (طلا پالا)

Self-hosted decision-support and analytics system for **Iranian 18-karat gold**: live prices, theoretical-vs-market premium, multi-horizon forecasts with uncertainty, explainable Buy/Hold/Sell signals, portfolio profit/loss tracking, and alerts.

> ⚠️ **Predictions are uncertain estimates for decision support — not financial advice.** Backtested performance does not guarantee future performance. See [docs/limitations.md](docs/limitations.md).

## What it does

- **Current market**: Iranian 18k gram price (toman), global XAU/USD, free-market USD/toman, theoretical 18k price from the ounce formula, and the local premium/discount with abnormal-premium alerts.
- **Forecasts**: separate models per horizon (1h → 30d, enabled only when data supports them), each with a point forecast, prediction interval, expected change, direction, confidence, regime, and the drivers behind it.
- **Signals**: explainable Strong Buy → Strong Sell score (0–100) combining forecast, trend, momentum, premium, volatility, costs, and data freshness — with supporting/conflicting factors, risks, and invalidation conditions spelled out.
- **Portfolio**: manual or CSV-imported holdings (e.g. from Hamrah Gold), current value, P/L, break-even, scenario analysis. No connection to Hamrah Gold accounts — by design.
- **Backtesting**: walk-forward, fee/spread/slippage-aware, benchmarked against buy-and-hold and moving-average strategies.

## Architecture

Five containers, nothing more: Go API (auth, portfolio, alerts, scheduler, serving) · Python FastAPI (data collection, features, models, backtests, signals) · React dashboard · PostgreSQL · Redis. Details: [docs/architecture.md](docs/architecture.md).

## Quick start

```bash
cp .env.example .env      # set POSTGRES_PASSWORD, JWT_SECRET, INTERNAL_API_TOKEN
docker compose up -d --build
docker compose exec api /app/createuser -email you@example.com -password 'a-strong-password' -role admin
docker compose exec prediction-service python -m app.seed.seed_history
make collect train predict signals
# open http://localhost:8088
```

Or in one step: `./scripts/init.sh you@example.com 'a-strong-password'`

## Documentation

| Topic | Doc |
|---|---|
| Architecture & data flow | [docs/architecture.md](docs/architecture.md) |
| Data sources (+ access dates, ToS notes) | [docs/data-sources.md](docs/data-sources.md) |
| Gold formula, rial/toman, premium | [docs/formula-and-units.md](docs/formula-and-units.md) |
| Models, validation, backtesting, signals | [docs/methodology.md](docs/methodology.md) |
| Reviewed GitHub repos & reused ideas | [docs/repo-review.md](docs/repo-review.md) |
| REST API | [docs/api.md](docs/api.md) · OpenAPI: `backend-go/docs/openapi.yaml` |
| Service contracts (internal) | [docs/CONTRACTS.md](docs/CONTRACTS.md) |
| Deployment (Linux) & commands | [docs/deployment.md](docs/deployment.md) |
| Configuration reference | [docs/configuration.md](docs/configuration.md) |
| Security design | [docs/security.md](docs/security.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Known limitations & disclaimer | [docs/limitations.md](docs/limitations.md) |

## Commands — what to run, when, and why

Run all of these from the project directory on the server (`/opt/iran-gold-predictor`). Everything listed under "Automatic" also runs on its own schedule — manual runs are for "I want it now".

### Daily operation (usually nothing to do)

| Command | What it does | When to run it |
|---|---|---|
| `make ps` | Shows every container + its health status | Whenever you wonder "is it running?" |
| `make logs SERVICE=api` | Live logs (also `prediction-service`, `frontend`, `postgres`, `redis`; omit SERVICE for all) | When something looks wrong |
| `make smoke` | Automated health probe of the whole stack | After any restart/update |

### Data & models (all automatic; manual = don't wait for the schedule)

| Command | What it does | Automatic schedule | Run manually when… |
|---|---|---|---|
| `make collect` | Fetches current prices from all enabled providers, validates, stores | every 10 min (`SCHEDULE_COLLECT_CRON`) | you want the freshest price right now |
| `make train` | Retrains all models per horizon, walk-forward validates, activates winners | daily 02:30 UTC | after seeding history, or after a big market shift |
| `make predict` | Regenerates features + forecasts for every enabled horizon | hourly | right after a manual `train` |
| `make signals` | Recomputes the Buy/Hold/Sell signal from latest forecasts | hourly (after predict) | right after a manual `predict` |
| `make backtest HORIZON=1d` | Runs a fee/spread-aware backtest for one horizon (also `3d`, `7d`…) | — (on demand) | you want to see how the strategy would have performed |
| `docker compose exec prediction-service python -m app.seed.seed_history` | Backfills years of daily history (TGJU + fallbacks) | — (one-time) | first install, or after wiping the database |

### Users & portfolio

| Command | Purpose |
|---|---|
| `make create-user EMAIL=a@b.c PASSWORD=pw ROLE=user` | Add a login (ROLE=admin for a second admin) |
| `make export-portfolio TOKEN=<jwt>` | Download your portfolio as CSV (get the JWT from the login response, or use the dashboard's export button) |

### Lifecycle

| Command | Purpose |
|---|---|
| `./scripts/init.sh email pass` | Full first-time setup: .env with random secrets → build → start → admin user → seed → first train |
| `make up` / `make down` | Start (rebuilding changed images) / stop; data survives `down` |
| `make migrate` | Force DB migrations (they also run at every api start) |
| `make update` | `git pull` + rebuild + restart (when deploying from git) |
| `make test-go` / `make test-python` | Unit test suites (need Go / Python installed) |
| `./tests/integration_api_test.sh` | Full API test against the running stack |

Changing schedules: edit the `SCHEDULE_*_CRON` lines in `.env` (UTC, standard cron), then `docker compose up -d api`. Example: collect every 5 minutes → `SCHEDULE_COLLECT_CRON=*/5 * * * *`.

## Testing

```bash
make test-go        # Go unit tests (no DB needed)
make test-python    # Python unit tests (providers use fixtures, no network)
make smoke          # Docker Compose smoke test
./tests/integration_api_test.sh   # full API integration test against the running stack
```

## Repository layout

```
backend-go/          Go API, scheduler, indicators, portfolio, alerts
prediction-python/   FastAPI service: providers, features, models, backtests, signals
frontend/            React + Vite dashboard (served by nginx)
database/migrations/ PostgreSQL schema (golang-migrate format)
scripts/             init / smoke test
tests/               Cross-service integration tests
docs/                All documentation
data/samples/        Example portfolio CSV
```

## License & ethics

For personal use. Market data is fetched politely (honest User-Agent, backoff, rate limits) from sources listed in [docs/data-sources.md](docs/data-sources.md); no CAPTCHA/auth bypassing, no scraping of private Hamrah Gold data.
