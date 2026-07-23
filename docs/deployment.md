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

## Updating

```bash
make update     # git pull --ff-only && docker compose up -d --build
make smoke
```

Migrations are forward-only and run automatically at api startup.
