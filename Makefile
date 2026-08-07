.PHONY: install lint test docker-up docker-down migrate run worker e2e-m2

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

# End-to-end M2 happy path: seeds Project/Task/Run in Postgres, then runs
# the orchestrator worker which calls the LLM endpoint (Anthropic-compatible).
# Requires `.env` with LLM_* vars set.
e2e-m2:
	set -a && . ./.env && set +a && \
	python -c "import asyncio; from uuid import uuid4; from orchestration.storage.db import session_scope; from orchestration.storage.models.project import Project; from orchestration.storage.models.task import Task; from orchestration.storage.models.run import Run; \
	async def main(): \
		async with session_scope() as s: \
			pid = uuid4(); \
			s.add(Project(id=pid, name='demo', repo_url='/tmp/repo')); \
			tid = uuid4(); s.add(Task(id=tid, project_id=pid, external_id='T1', source_type='csv', title='Add hello world', description='Print hello')); \
			rid = uuid4(); s.add(Run(id=rid, project_id=pid, task_id=tid, flow_uuid=uuid4(), idempotency_key=str(rid))); \
			print(rid); \
	asyncio.run(main())" \
	| tail -1 | xargs -I {} python -m orchestration.workers.orchestrator_worker {}
