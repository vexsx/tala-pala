# Architecture Overview

Iran Gold Predictor is a decision-support and analytics system for Iranian 18-karat gold. It is **not** a trading system and does not guarantee outcomes.

## High-level design

```mermaid
flowchart LR
    subgraph External["External data sources"]
        TGJU[TGJU\nIranian gold & FX]
        ALAN[Alanchand\nfallback]
        NAV[Navasan API\noptional, keyed]
        YAH[Yahoo Finance\nXAU, silver, oil, DXY]
        STQ[Stooq CSV\nfallback]
    end

    subgraph Docker["Docker internal network"]
        PY[prediction-service\nPython FastAPI :8500]
        API[api\nGo :8080]
        FE[frontend\nReact + nginx :80]
        PG[(PostgreSQL)]
        RD[(Redis)]
    end

    Browser((Browser)) --> FE
    FE -- "/api/v1/*" --> API
    API -- SQL --> PG
    API -- locks/cache --> RD
    API -- "X-Internal-Token\n/internal/*" --> PY
    PY -- SQL --> PG
    TGJU & ALAN & NAV & YAH & STQ --> PY
```

## Components

| Service | Language | Responsibility |
|---|---|---|
| `api` | Go | Public REST API, JWT auth, portfolio, alerts, technical indicators, DB migrations at startup, cron scheduler with Redis distributed locks |
| `prediction-service` | Python | Data providers with fallback, normalization & validation, gold formula, feature engineering, model training/selection (walk-forward), predictions with intervals, backtesting, signal generation |
| `frontend` | React + Vite | Dashboard (Overview, Forecast, Technical, Drivers, Portfolio, Alerts, Models); nginx serves static files and proxies `/api/` |
| `postgres` | — | Persistent store: prices, features, models, predictions, signals, users, portfolio, alerts, audit |
| `redis` | — | Distributed job locks, coordination |

Both services still expose `/metrics` (Go) and `/internal/metrics` (Python) in Prometheus text format on the internal network, so a metrics stack can be pointed at them later without code changes — none is deployed by default.

## Data flow

```mermaid
sequenceDiagram
    participant S as Go scheduler (cron + Redis lock)
    participant P as prediction-service
    participant DB as PostgreSQL
    participant U as User (browser)
    S->>P: POST /internal/collect
    P->>P: fetch providers (priority, fallback, retry)
    P->>P: validate, detect outliers, normalize rial→toman
    P->>DB: raw_observations + prices
    S->>P: POST /internal/features/generate
    P->>DB: feature_snapshots (point-in-time correct)
    S->>P: POST /internal/predict
    P->>DB: predictions (point, interval, confidence, drivers)
    S->>P: POST /internal/signals/generate
    P->>DB: signals (score, explanation, risks)
    S->>S: evaluate user alerts → alert_events
    U->>DB: (via Go API) prices, predictions, signals, portfolio
```

Key properties:

- **Go never computes forecasts**; it reads what Python wrote to Postgres, so user requests are fast and independent of model latency.
- **Python is never exposed publicly**; only the Go service can reach it, authenticated with `INTERNAL_API_TOKEN`.
- **Only one scheduler instance runs a job at a time** — each job takes a Redis `SET NX PX` lock (`lock:job:<name>`), so extra `api` replicas are safe.
- **Migrations** run automatically when `api` starts (golang-migrate, files from `database/migrations/`); `prediction-service` waits for `api` to be healthy.

## Database model (summary)

See `database/migrations/` for authoritative DDL.

```mermaid
erDiagram
    data_providers ||--o{ raw_observations : produces
    raw_observations ||--o{ prices : normalized_into
    prices ||--o{ feature_snapshots : aggregated_into
    feature_snapshots ||--o{ predictions : feeds
    model_versions ||--o{ predictions : generates
    training_runs ||--o{ model_versions : registers
    predictions ||--o{ signals : informs
    users ||--o{ portfolio_transactions : owns
    users ||--o{ alerts : configures
    alerts ||--o{ alert_events : triggers
    users ||--o{ audit_logs : recorded_in
```

Time-series tables (`prices`, `raw_observations`, `predictions`) carry composite indexes on `(symbol, observed_at DESC)`-style keys. TimescaleDB was considered and rejected for now: at one row per symbol per 10 minutes (~50k rows/symbol/year) plain B-tree indexes are more than sufficient, and avoiding the extension keeps deployment simple. The migration path is documented in `docs/limitations.md`.

## Why this shape

- Separating collection/ML (Python) from serving (Go) lets each use its best ecosystem and fail independently — if the model service dies, prices and portfolio still work, and staleness is surfaced honestly rather than hidden.
- All cross-service state goes through Postgres, making every prediction and signal auditable and replayable (`feature_snapshots` records exactly what a model saw).
