.PHONY: help format test build local-up cache-enabled-local local-down load

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_-]+:.*## / {printf "%-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

format: ## Format Go source
	gofmt -w $$(find cmd internal -name '*.go' -type f)

test: ## Run unit tests
	go test ./...

build: ## Build all Go packages
	go build ./...

local-up: ## Start the slow-database baseline with caching disabled
	CACHE_ENABLED=false docker compose up --build -d

cache-enabled-local: ## Start or recreate the local stack with caching enabled
	CACHE_ENABLED=true docker compose --profile cache up --build -d

local-down: ## Stop the local stack and remove its demo volume
	docker compose --profile cache down --volumes

load: ## Run the repeatable catalogue load scenario
	k6 run load/catalog.js
