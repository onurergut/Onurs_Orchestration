.PHONY: install lint test docker-up docker-down migrate run worker

install:
	pip install -e ".[dev]"

lint:
	ruff check src tests
	mypy src

test:
	pytest

docker-up:
	docker compose up -d postgres redis

docker-down:
	docker compose down

migrate:
	alembic upgrade head

run:
	uvicorn orchestration.main:app --reload --host 0.0.0.0 --port 8000

worker:
	python -m orchestration.workers.orchestrator_worker
