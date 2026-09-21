# Nightorder dev targets. Requires: uv, docker, temporal CLI.
SHELL := /bin/bash

.PHONY: install infra infra-down worker api test test-unit test-e2e demo clean

install:
	uv sync

infra:            ## postgres + qdrant (docker) + temporal dev server (background)
	docker compose up -d postgres qdrant
	@pgrep -f "temporal server start-dev" >/dev/null || \
		(nohup temporal server start-dev --headless --db-filename .temporal-dev.db > .temporal-dev.log 2>&1 & \
		 echo "temporal dev server starting (log: .temporal-dev.log)"; sleep 3)

infra-head:            ## same but temporal dev server with UI on :8233
	docker compose up -d postgres qdrant
	@pgrep -f "temporal server start-dev" >/dev/null || \
		(nohup temporal server start-dev --db-filename .temporal-dev.db > .temporal-dev.log 2>&1 & \
		 echo "temporal dev server starting (log: .temporal-dev.log)"; sleep 3)

infra-down:
	docker compose down
	-pkill -f "temporal server start-dev"

worker:           ## run the Temporal worker (loads all installed extensions)
	uv run nightorder-worker

api:              ## run the FastAPI control plane on :8400
	uv run nightorder-api

relay:            ## outbox relay (Qdrant indexing; Kafka later)
	uv run nightorder-relay

test-unit:
	uv run pytest tests/unit -q

test-e2e:         ## full local e2e (starts its own worker+api against infra)
	uv run pytest tests/e2e -q -s

test: test-unit test-e2e

clean:
	rm -f .temporal-dev.db .temporal-dev.log

clean-test-data:  ## purge e2e-* test projects from the local DB
	./scripts/purge-test-projects.sh
