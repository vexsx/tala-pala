# Iran Gold Predictor — operational entry points.

COMPOSE = docker compose

.PHONY: help setup up down build logs ps migrate create-user collect train predict \
        signals backtest export-portfolio test test-go test-python smoke update

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

test: test-go test-python ## Run all local test suites

test-go: ## Go unit tests
	cd backend-go && go vet ./... && go test ./...

test-python: ## Python unit tests
	cd prediction-python && python -m pytest -q

smoke: ## Docker Compose smoke test
	bash scripts/smoke_test.sh

update: ## Pull latest code, rebuild, restart (run from git checkout)
	git pull --ff-only && $(COMPOSE) up -d --build && $(COMPOSE) ps
