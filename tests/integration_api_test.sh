#!/usr/bin/env bash
# End-to-end API integration test against a RUNNING stack (docker compose up -d first).
# Usage: ./tests/integration_api_test.sh [base_url]
# Creates a throwaway user via the api container (admin CLI), exercises the public API.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${1:-http://localhost:${FRONTEND_PORT:-8088}}"
EMAIL="itest-$(date +%s)@local.test"
PASS="Integration-Test-Pass-1"
FAIL=0

step() { echo; echo "== $*"; }
assert_contains() { echo "$1" | grep -q "$2" || { echo "ASSERT FAILED: expected '$2' in: $(echo "$1" | head -c 300)"; FAIL=1; }; }

step "health & readiness"
assert_contains "$(curl -fsS "$BASE/api/v1/health")" '"ok"'
assert_contains "$(curl -fsS "$BASE/api/v1/readiness")" '"ok"'

step "create test user via CLI"
docker compose exec -T api /app/createuser -email "$EMAIL" -password "$PASS" -role user

step "login"
LOGIN=$(curl -fsS -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
assert_contains "$LOGIN" '"token"'
TOKEN=$(echo "$LOGIN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null \
     || echo "$LOGIN" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
AUTH=(-H "Authorization: Bearer $TOKEN")

step "wrong password rejected"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$EMAIL\",\"password\":\"wrong-password-1\"}")
[ "$CODE" = "401" ] || { echo "ASSERT FAILED: bad login returned $CODE"; FAIL=1; }

step "prices & market"
assert_contains "$(curl -fsS "${AUTH[@]}" "$BASE/api/v1/prices/current")" '"prices"'
assert_contains "$(curl -fsS "${AUTH[@]}" "$BASE/api/v1/market/summary")" 'IR_GOLD_18K\|premium\|signal'
curl -fsS "${AUTH[@]}" "$BASE/api/v1/market/indicators?days=90" >/dev/null || { echo "indicators failed"; FAIL=1; }
curl -fsS "${AUTH[@]}" "$BASE/api/v1/predictions" >/dev/null || { echo "predictions failed"; FAIL=1; }
curl -fsS "${AUTH[@]}" "$BASE/api/v1/signals/current" >/dev/null || echo "  (no signal yet — acceptable on a fresh stack)"

step "portfolio lifecycle"
TX=$(curl -fsS "${AUTH[@]}" -X POST "$BASE/api/v1/portfolio/transactions" -H 'Content-Type: application/json' \
  -d '{"tx_type":"buy","grams":2.5,"karat":18,"price_per_gram":6000000,"currency":"IRT","fees":100000,"tx_date":"2026-01-15","notes":"itest"}')
assert_contains "$TX" '"id"'
PF=$(curl -fsS "${AUTH[@]}" "$BASE/api/v1/portfolio")
assert_contains "$PF" '"invested"'
step "portfolio CSV import/export"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST "$BASE/api/v1/portfolio/import" \
  -F "file=@data/samples/portfolio_example.csv")
[ "$CODE" = "200" ] || { echo "ASSERT FAILED: import returned $CODE"; FAIL=1; }
EXPORT=$(curl -fsS "${AUTH[@]}" "$BASE/api/v1/portfolio/export")
assert_contains "$EXPORT" 'tx_type,grams'

step "alerts lifecycle"
AL=$(curl -fsS "${AUTH[@]}" -X POST "$BASE/api/v1/alerts" -H 'Content-Type: application/json' \
  -d '{"alert_type":"price_above","condition":{"threshold":9000000},"cooldown_minutes":60}')
assert_contains "$AL" '"id"'
assert_contains "$(curl -fsS "${AUTH[@]}" "$BASE/api/v1/alerts")" 'price_above'

step "admin denied for regular user"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST "$BASE/api/v1/admin/jobs/collect")
[ "$CODE" = "403" ] || { echo "ASSERT FAILED: admin endpoint returned $CODE for user role"; FAIL=1; }

echo
if [ "$FAIL" -eq 0 ]; then echo "INTEGRATION TESTS PASSED"; else echo "INTEGRATION TESTS FAILED"; exit 1; fi
