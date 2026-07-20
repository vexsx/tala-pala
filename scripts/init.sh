#!/usr/bin/env bash
# First-time setup: creates .env with random secrets, builds and starts the stack,
# creates the first (admin) user, and triggers initial data collection + training.
# Usage: ./scripts/init.sh admin@example.com 'a-strong-password'
set -euo pipefail
cd "$(dirname "$0")/.."

EMAIL="${1:?usage: init.sh <admin-email> <admin-password>}"
PASSWORD="${2:?usage: init.sh <admin-email> <admin-password>}"

if [ ! -f .env ]; then
  echo "[init] creating .env from .env.example with random secrets ..."
  cp .env.example .env
  rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand)|" .env
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(rand)|" .env
  sed -i "s|^INTERNAL_API_TOKEN=.*|INTERNAL_API_TOKEN=$(rand)|" .env
else
  echo "[init] .env already exists — keeping it."
fi

echo "[init] building and starting the stack ..."
docker compose up -d --build

echo "[init] waiting for the API to become healthy ..."
for i in $(seq 1 30); do
  docker compose exec -T api wget -qO- http://localhost:8080/api/v1/health >/dev/null 2>&1 && break
  sleep 5
done

echo "[init] creating admin user $EMAIL ..."
docker compose exec -T api /app/createuser -email "$EMAIL" -password "$PASSWORD" -role admin || \
  echo "[init] user may already exist — continuing."

echo "[init] seeding historical data (bundled samples + free global sources) ..."
docker compose exec -T prediction-service python -m app.seed.seed_history || \
  echo "[init] seed reported issues — check logs; live collection will still accumulate data."

echo "[init] triggering first collection, features, training and prediction ..."
TOKEN=$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)
run_job() {
  docker compose exec -T api wget -qO- \
    --header "Content-Type: application/json" \
    --header "X-Internal-Token: $TOKEN" \
    --post-data "${2:-{}}" "http://prediction-service:8500/internal/$1" || echo "  ($1 failed — see logs)"
  echo
}
run_job collect '{"jobs":[]}'
run_job features/generate '{}'
run_job train '{"horizons":[]}'
run_job predict '{"horizons":[]}'
run_job signals/generate '{}'

echo
echo "[init] DONE. Open http://localhost:${FRONTEND_PORT:-8088} and log in as $EMAIL"
echo "[init] Reminder: predictions are uncertain estimates, not financial advice."
