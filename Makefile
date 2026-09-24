# Iran Gold Predictor — operational entry points.

COMPOSE = docker compose

.PHONY: help setup up down build logs ps migrate create-user collect train predict \
        signals backtest export-portfolio test test-go test-python smoke update \
        refresh-offserver refresh-equities refresh-cpi

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  %-18s %s\n", $$1, $$2}'

setup: ## First-time setup: make setup EMAIL=you@example.com PASSWORD=changeme
	bash scripts/init.sh $(EMAIL) $(PASSWORD)

up: ## Start the full stack
	$(COMPOSE) up -d --build

down: ## Stop the stack (data volumes preserved)
	$(COMPOSE) down

build: ## Rebuild images
	$(COMPOSE) build

logs: ## Tail logs (SERVICE=api to filter)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

ps: ## Show service status + health
	$(COMPOSE) ps

migrate: ## Run DB migrations (they run automatically at every api startup)
	$(COMPOSE) restart api

migrate-force: ## Recover a dirty migration state: make migrate-force VERSION=n
	@test -n "$(VERSION)" || { echo "usage: make migrate-force VERSION=<last known-good version>"; exit 1; }
	$(COMPOSE) run --rm --no-deps api -force-migration-version $(VERSION)

backup: ## Dump the database to ./backups (see scripts/backup.sh for cron setup)
	sh scripts/backup.sh

create-user: ## Create a user: make create-user EMAIL=a@b.c PASSWORD=pw ROLE=user
	$(COMPOSE) exec api /app/createuser -email $(EMAIL) -password $(PASSWORD) -role $(or $(ROLE),user)

collect: ## Trigger data collection now
	$(COMPOSE) exec api wget -qO- --header "Content-Type: application/json" --header "X-Internal-Token: $$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)" --post-data '{"jobs":[]}' http://prediction-service:8500/internal/collect

train: ## Trigger model training now
	$(COMPOSE) exec api wget -qO- --header "Content-Type: application/json" --header "X-Internal-Token: $$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)" --post-data '{"horizons":[]}' http://prediction-service:8500/internal/train

predict: ## Generate predictions now
	$(COMPOSE) exec api wget -qO- --header "Content-Type: application/json" --header "X-Internal-Token: $$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)" --post-data '{"horizons":[]}' http://prediction-service:8500/internal/predict

signals: ## Regenerate the Buy/Hold/Sell signal now
	$(COMPOSE) exec api wget -qO- --header "Content-Type: application/json" --header "X-Internal-Token: $$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)" --post-data '{}' http://prediction-service:8500/internal/signals/generate

backtest: ## Run a backtest: make backtest HORIZON=1d
	$(COMPOSE) exec api wget -qO- --header "Content-Type: application/json" --header "X-Internal-Token: $$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)" --post-data '{"horizon":"$(or $(HORIZON),1d)"}' http://prediction-service:8500/internal/backtest

export-portfolio: ## Export portfolio CSV: make export-portfolio TOKEN=<jwt> [OUT=portfolio.csv]
	curl -fsS -H "Authorization: Bearer $(TOKEN)" http://localhost:$${FRONTEND_PORT:-8088}/api/v1/portfolio/export -o $(or $(OUT),portfolio.csv) && echo "wrote $(or $(OUT),portfolio.csv)"

# --- the manually refreshed datasets -----------------------------------------
#
# RUN THESE SOMEWHERE THAT CAN REACH THE SOURCES. NOT ON THE SERVER.
#
# Every other target in this file is run on the production host. These three
# are the exception, and it is not a preference — the sources are unreachable
# from that host, measured rather than assumed:
#
#   amar.org.ir     DNS resolves, TCP connects on BOTH 443 and 80, and then
#                   nothing answers: no TLS handshake completes at 1.2 or 1.3,
#                   and plain HTTP returns nothing either. The connection is
#                   accepted and the payload dropped, which is application-layer
#                   filtering. (Diagnosed 2026-09-09.)
#   cdn.tsetmc.com  TCP 443 fails OUTRIGHT on all three addresses, http=000.
#                   A harder block than the SCI one. (Diagnosed 2026-09-10.)
#
# Both answer normally from outside that network, so the scripts run on a
# laptop or any reachable host: they download, verify, scp the payloads to the
# server over the EXISTING ssh access, and trigger the ingest inside the
# compose network. Nothing new is exposed publicly.
#
# Consequence worth stating plainly: these datasets do not refresh themselves,
# and nothing on the server will ever refresh them. Run refresh-offserver at
# least WEEKLY. If it stops, EquityBarsStale / SciCpiIngestStale in
# observability/alerts.yml fire, and GET /api/v1/stocks/screen reports the age
# in its `data_age` block — but the fix is always a human running this target.
#
# Pass extra script flags with ARGS, e.g.:
#   make refresh-equities ARGS="--dry-run"
#   make refresh-equities ARGS="--ins-code 46348559193224090"
#   make refresh-cpi ARGS="--host ubuntu@1.2.3.4"

refresh-equities: ## Refresh Tehran equity bars from TSETMC — run OFF the server (ARGS=...)
	python3 scripts/tsetmc_fetch.py $(ARGS)

refresh-cpi: ## Refresh SCI CPI from amar.org.ir — run OFF the server (ARGS=...)
	python3 scripts/sci_fetch.py $(ARGS)

# Sequenced through $(MAKE) rather than as prerequisites so `make -j` cannot
# run them concurrently: both scp to the same host and both trigger an ingest.
# A failure in the first stops the second, which is why the two targets above
# exist separately — recover by running the one that failed.
refresh-offserver: ## Refresh BOTH manual datasets (equities, then CPI) — run OFF the server
	@echo "These fetches must run where amar.org.ir and cdn.tsetmc.com are reachable."
	@echo "Neither is reachable from the production host; see the Makefile comment."
	@$(MAKE) --no-print-directory refresh-equities
	@$(MAKE) --no-print-directory refresh-cpi

test: test-go test-python ## Run all local test suites

test-go: ## Go unit tests
	cd backend-go && go vet ./... && go test ./...

test-python: ## Python unit tests
	cd prediction-python && python -m pytest -q

smoke: ## Docker Compose smoke test
	bash scripts/smoke_test.sh

update: ## Pull latest code, rebuild, restart (run from git checkout)
	git pull --ff-only && GIT_COMMIT=$$(git rev-parse --short HEAD) $(COMPOSE) up -d --build && $(COMPOSE) ps
	@$(MAKE) --no-print-directory verify-deploy

verify-deploy: ## Fail if ANY running service predates the repo HEAD
	@repo=$$(git rev-parse --short HEAD); bad=0; \
	pred=$$($(COMPOSE) exec -T prediction-service sh -c 'echo $$BUILD_COMMIT' 2>/dev/null | tr -d "\r\n"); \
	api=$$($(COMPOSE) exec -T api sh -c 'echo $$BUILD_COMMIT' 2>/dev/null | tr -d "\r\n"); \
	fe=$$($(COMPOSE) exec -T frontend sh -c 'cat /usr/share/nginx/html/build-info.json 2>/dev/null' \
	      | sed -n 's/.*"build_commit":"\([^"]*\)".*/\1/p' | tr -d "\r\n"); \
	for pair in "prediction-service:$$pred" "api:$$api" "frontend:$$fe"; do \
	  svc=$${pair%%:*}; got=$${pair#*:}; \
	  if [ "$$got" = "$$repo" ]; then echo "  ok    $$svc $$got"; \
	  else echo "  STALE $$svc $${got:-unknown} (repo $$repo)" >&2; bad=1; fi; \
	done; \
	if [ $$bad -eq 0 ]; then echo "deploy OK: all services run $$repo"; \
	else echo "DEPLOY MISMATCH - run: make update" >&2; exit 1; fi
