.PHONY: up down reset test test-integration lint fault-db-down fault-clear status

up:                ## start the sandbox
	docker compose up -d --build --wait

down:              ## stop the sandbox (keeps data)
	docker compose down

reset:             ## stop and delete all sandbox data
	docker compose down -v

fault-db-down:     ## INC-001: stop app-db so orders-api loses its database
	docker compose stop app-db

fault-clear:       ## manual recovery, outside the agent
	docker compose start app-db

status:
	docker compose ps
	@curl -s -w '  (HTTP %{http_code})\n' http://localhost:8101/health || true

test:              ## unit and workflow tests: no Docker, no API key, no cost
	uv run pytest -m "not integration"

test-integration:  ## real outage tests against the running sandbox
	uv run pytest -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .
