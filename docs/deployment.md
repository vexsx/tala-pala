# Linux Deployment Guide

## Prerequisites
- Linux server (2+ vCPU, 4 GB RAM, 20 GB disk recommended)
- Docker Engine ≥ 24 and the Docker Compose plugin

## Quick start

```bash
git clone <your-repo> iran-gold-predictor
cd iran-gold-predictor

# one-command setup: creates .env with random secrets, builds, starts,
# creates the admin user, seeds history, runs first collection/training
./scripts/init.sh admin@example.com 'a-strong-password'

# open http://<server>:8088 and log in
```

Manual equivalent:

```bash
cp .env.example .env        # then edit: set POSTGRES_PASSWORD, JWT_SECRET, INTERNAL_API_TOKEN
docker compose up -d --build
docker compose exec api /app/createuser -email admin@example.com -password 'a-strong-password' -role admin
docker compose exec prediction-service python -m app.seed.seed_history
make collect train predict signals
```

## Command reference

| Action | Command |
|---|---|
| Start / rebuild | `make up` (= `docker compose up -d --build`) |
| Stop | `make down` |
| Status + health | `make ps` |
| Logs | `make logs` or `make logs SERVICE=api` |
| Run migrations | automatic at every api startup; force: `make migrate` (restarts api) |
| Create a user | `make create-user EMAIL=a@b.c PASSWORD=pw ROLE=user` |
| Trigger data collection | `make collect` |
| Trigger training | `make train` |
| Generate predictions | `make predict` |
| Regenerate signal | `make signals` |
| Run a backtest | `make backtest HORIZON=1d` |
| Export portfolio CSV | `make export-portfolio TOKEN=<jwt>` |
| Update deployment | `make update` |
| Smoke test | `make smoke` |
| Refresh equity bars | `make refresh-equities` — **not on the server**, see below |
| Refresh SCI CPI | `make refresh-cpi` — **not on the server**, see below |
| Refresh both | `make refresh-offserver` — **not on the server**, see below |

## Exposing it beyond localhost

The dashboard is published on `FRONTEND_PORT` (default 8088); Postgres, Redis, the Go API, and the Python service stay on the internal Docker network. If you expose the app to the internet, put any TLS-terminating proxy you already run (Caddy, nginx, Traefik) in front of port 8088 — the app itself needs no changes. Do not expose it over plain HTTP publicly.

## Database backups

Bundled backup tooling: `make backup` (or `scripts/backup.sh`) dumps via the running postgres container into `./backups/` with retention, and mirrors off-host when `BACKUP_RSYNC_TARGET` is set. Install the cron shown in the script header. The old manual approach; a one-liner covers it when needed:

```bash
docker compose exec -T postgres pg_dump -U goldpred -d goldpred -Fc > goldpred_$(date -u +%Y%m%dT%H%M%SZ).dump
# restore: docker compose exec -T postgres pg_restore -U goldpred -d goldpred --clean --if-exists --no-owner < file.dump
```

## Logs & rotation
All services log JSON to stdout. Configure the Docker daemon's log rotation in `/etc/docker/daemon.json`:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }
```

then `sudo systemctl restart docker`.

## Scheduled jobs
The Go service embeds the scheduler (UTC cron, configurable via `SCHEDULE_*_CRON` in `.env`); jobs use Redis locks so scaling `api` replicas won't duplicate work. Default cadence: collect every 10 min, predict/signals/evaluate hourly, train daily 02:30 UTC, alert evaluation every 5 min, cleanup daily 04:00 UTC.

## Data refresh: two classes, and only one looks after itself

This is the operational fact most likely to bite a new operator, because nothing about a running deployment makes it visible. **Some of this platform's data updates itself and some of it only updates when a human runs a script.**

| Dataset | Class | How it refreshes | Cadence |
|---|---|---|---|
| Gold (IR_GOLD_18K), FX (USD_IRT), coins, XAUUSD and the other global series | **self-updating** | `collect` cron, on the server | every 10 min, automatic |
| Predictions, signals, model training, retention | **self-updating** | `predict` / `signals` / `train` / `cleanup` crons, on the server | hourly to nightly, automatic |
| World Bank + IMF WEO economic series | **self-updating** | `economic` job on the prediction service | daily 03:40 UTC, automatic |
| **Tehran equity bars (TSETMC)** | **manual** | `make refresh-equities`, run **off** the server | **run it at least weekly** |
| **SCI CPI (headline, housing, rent, vehicle)** | **manual** | `make refresh-cpi`, run **off** the server | **run it monthly**, a week or two after each Jalali month ends |

### Why the last two cannot be crons

Neither source is reachable from the production host. This was measured, not assumed:

* **`amar.org.ir`** — DNS resolves (217.218.11.77) and TCP connects on **both** 443 and 80, and then nothing answers: no TLS handshake completes at 1.2 or 1.3, and plain HTTP returns nothing either. The connection is accepted and the payload dropped, which is application-layer filtering rather than a TLS misconfiguration. (Diagnosed 2026-09-09.)
* **`cdn.tsetmc.com`** — TCP 443 fails **outright** on all three addresses, `http=000`. A harder block than the SCI one. (Diagnosed 2026-09-10.)

Both answer normally from outside that network. So `scripts/sci_fetch.py` and `scripts/tsetmc_fetch.py` run somewhere that can reach them — a laptop, or any other host — download and verify the payloads, `scp` them to the server over the **existing** SSH access, and trigger the ingest inside the compose network. Nothing new is exposed publicly, and the roster of what to fetch is read from the server rather than hardcoded, so widening it stays an `INSERT`.

### What to run

```bash
# from a machine that can reach amar.org.ir and cdn.tsetmc.com — NOT the server
make refresh-offserver                       # both, equities first
make refresh-equities                        # equities alone
make refresh-cpi                             # CPI alone
make refresh-equities ARGS="--dry-run"       # download and verify, change nothing
make refresh-cpi ARGS="--host ubuntu@1.2.3.4"
```

### How you find out when it has stopped

Until September 2026 the answer was "you don't". On 2026-09-24 the newest equity bar in production was 2026-09-09 — fifteen days — and the screener had been serving those prices under a window labelled with the current date the whole time. The only reason anyone knew is that a human went looking. Three things now report it:

* **Alerts.** `EquityBarsStale` (no new bar across the roster for 10 days), `EquityInstrumentBarsStale` (one symbol left behind while the rest keep updating, 30 days), and `SciCpiIngestStale` (no new SCI observation for 45 days), all in `observability/alerts.yml` with their bounds justified in the annotations and the command to run in the description. They are self-gating: a deployment that has never ingested produces no series and never alerts.
* **Metrics.** `talapala_api_last_equity_bar_roster_timestamp_seconds`, `talapala_api_last_equity_bar_timestamp_seconds{symbol}` and `talapala_api_last_economic_observation_timestamp_seconds{provider}`, refreshed from Postgres every 5 minutes by the Go freshness job.
* **The API itself.** `GET /api/v1/stocks` and `GET /api/v1/stocks/screen` carry a top-level `data_age` block — the newest stored trade date, its age in days, a `stale` flag against a stated 10-day bound, and a prose `warning` naming `make refresh-equities`. The age is on the payload rather than left for a client to derive, because a client that never does the subtraction is exactly the one that renders the stale number under today's heading.

The bounds are not arbitrary. The Tehran exchange trades Saturday to Wednesday, so the newest bar is routinely 2–3 days old over a normal weekend and about 5 across a holiday-extended one; a 24-hour bound would page every weekend. SCI publishes monthly and weeks in arrears, so its bound is measured in weeks. The annual Nowruz closure is the one known false positive for the equity rule — silence it for that window rather than loosening the bound for the rest of the year.

## Updating

```bash
make update     # git pull --ff-only && docker compose up -d --build
make smoke
```

Migrations are forward-only and run automatically at api startup.
