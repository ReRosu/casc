.PHONY: up down logs test test-unit test-integration lint demo migrate

up:            ## поднять всё: postgres, redis, миграции, api, worker, scheduler, эмулятор
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f api worker emulator

migrate:
	poetry run alembic upgrade head

test-unit:
	poetry run pytest tests/unit -q

test-integration:  ## нужен Postgres: docker compose up -d postgres
	poetry run pytest tests/integration -q

test: lint
	poetry run pytest -q

lint:
	poetry run ruff check app tests

demo:          ## сценарии каскада против эмулятора (после make up)
	./scripts/demo.sh
