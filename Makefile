.PHONY: e2e e2e-down e2e-report test

# Auto-detect container runtime: prefer podman, fall back to docker
CONTAINER_ENGINE ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE_CMD = $(CONTAINER_ENGINE) compose

## Run E2E browser tests (Playwright in containers)
e2e:
	@$(CONTAINER_ENGINE) run --rm -v "$(CURDIR)/e2e-report:/data" alpine rm -rf /data/* 2>/dev/null || true
	$(COMPOSE_CMD) -f docker-compose.e2e.yml up --build --abort-on-container-exit --exit-code-from e2e; \
	EXIT_CODE=$$?; \
	$(COMPOSE_CMD) -f docker-compose.e2e.yml down -v; \
	echo ""; \
	echo "HTML report: e2e-report/report.html"; \
	echo "Screenshots: e2e-report/traces/"; \
	echo "Run 'make e2e-report' to view in browser."; \
	exit $$EXIT_CODE

## Serve the E2E HTML report in a browser (avoids file:// security errors)
e2e-report:
	@test -f e2e-report/report.html || { echo "No report found. Run 'make e2e' first."; exit 1; }
	@echo "Serving report at http://localhost:9323/report.html  (Ctrl+C to stop)"
	@cd e2e-report && python3 -m http.server 9323

## Tear down E2E containers (cleanup after failure)
e2e-down:
	$(COMPOSE_CMD) -f docker-compose.e2e.yml down -v

## Run unit/integration tests
test:
	pytest tests/ -q
