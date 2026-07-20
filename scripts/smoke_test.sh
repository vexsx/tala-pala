#!/usr/bin/env bash
# Docker Compose smoke test: brings the stack up and probes every service.
# Usage: ./scripts/smoke_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${FRONTEND_PORT:-8088}"
FAIL=0

check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  [ok]   $name"
  else
    echo "  [FAIL] $name"
    FAIL=1
  fi
}

echo "[smoke] waiting for services to become healthy (max 180s) ..."
for i in $(seq 1 36); do
  UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Health}}' | grep -cv 'healthy' || true)
  [ "$UNHEALTHY" -eq 0 ] && break
  sleep 5
done
docker compose ps

echo "[smoke] probing endpoints ..."
check "frontend serves index"        "curl -fsS http://localhost:${PORT}/ | grep -qi '<div id='"
check "api health via frontend"      "curl -fsS http://localhost:${PORT}/api/v1/health | grep -q ok"
check "api readiness"                "curl -fsS http://localhost:${PORT}/api/v1/readiness | grep -q ok"
check "unauth request rejected"      "curl -s -o /dev/null -w '%{http_code}' http://localhost:${PORT}/api/v1/prices/current | grep -q 401"
check "postgres accepts connections" "docker compose exec -T postgres pg_isready"
check "redis responds to ping"       "docker compose exec -T redis redis-cli ping | grep -q PONG"
check "prediction service healthy"   "docker compose exec -T api wget -qO- http://prediction-service:8500/internal/health | grep -q ok"

if [ "$FAIL" -eq 0 ]; then
  echo "[smoke] ALL CHECKS PASSED"
else
  echo "[smoke] FAILURES DETECTED — inspect: docker compose logs --tail=100"
  exit 1
fi
