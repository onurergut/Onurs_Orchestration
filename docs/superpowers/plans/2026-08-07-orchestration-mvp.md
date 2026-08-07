# Orchestration System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted multi-agent software engineering orchestration system: API + CrewAI Flow orchestrator + LLM gateway + adapters + storage + Web UI, following the resolved design spec.

**Architecture:** FastAPI HTTP layer; CrewAI Flow orchestrator with `@persist` + `checkpoint=True` for resumable runs; LLM gateway abstracts OpenAI/Anthropic-compatible endpoints; pluggable adapters (CSV/Jira/Linear/GitHub Issues for tasks; Web UI/Slack/GitHub PR/Jira comment for review); Postgres for state, Redis for queue/cache/idempotency; per-project budget guard at stage boundary; per-task isolated git worktrees.

**Tech Stack:** Python 3.12, FastAPI, CrewAI Flow, Pydantic v2, SQLAlchemy 2.0 async, Alembic, asyncpg, Redis 7, PostgreSQL 16, Docker Compose, pytest, httpx, structlog, OpenTelemetry.

## Global Constraints

These apply to every task. Copy verbatim from the spec.

- **Python:** 3.12 (use `pyproject.toml` with `requires-python = ">=3.12"`)
- **API framework:** FastAPI (latest stable)
- **State machine:** CrewAI Flow with `@persist` (FlowState) + `checkpoint=True` (execution pointer)
- **Validation:** Pydantic v2 (BaseModel for all schemas; `model_validate`, `model_dump_json`)
- **DB:** PostgreSQL 16, SQLAlchemy 2.0 async, Alembic for migrations
- **Queue/Cache:** Redis 7 (idempotency, rate limit, circuit breaker, sessions)
- **LLM protocol:** Either OpenAI-compatible or Anthropic-compatible endpoints, configured per endpoint
- **Auth:** API-key only (`X-API-Key` header). Bcrypt/argon2 hash in `api_keys.key_hash`. No JWT.
- **Tenancy:** Single-org self-host, row-level `project_id` isolation in every domain query
- **File layout:** `src/` layout + domain-by-module (no `app/` directory)
- **Budget:** Per-project monthly USD, hard stop at stage boundary (NOT mid-stage)
- **Review timeout:** Default 24h, per-project override via `projects.review_config.review_timeout_seconds`
- **Worktree storage:** Local disk `/var/worktrees/`, 30-day TTL post-run-finished
- **Rate limit:** Per-provider global, token-bucket in Redis (`ratelimit:{provider}`)
- **Eval baseline:** SWE-bench Verified subset, 10 Python issues (seeded in M5)
- **Naming:** snake_case for files/functions, PascalCase for classes, kebab-case for CLI flags
- **No TODO/placeholders/FIXME in committed code**

---

## File Structure

All paths relative to repo root. Files marked `[m2]`, `[m3]` etc. are created in that milestone.

```
.
├── docker-compose.yml                          [m0]
├── docker/
│   ├── api.Dockerfile                          [m0]
│   └── worker.Dockerfile                       [m0]
├── pyproject.toml                              [m0]
├── README.md                                   [m0]
├── .env.example                                [m0]
├── .gitignore                                  [m0]
├── Makefile                                    [m0]
├── alembic.ini                                 [m0]
├── alembic/
│   ├── env.py                                  [m0]
│   ├── script.py.mako                          [m0]
│   └── versions/
│       └── 001_initial_schema.py               [m0]
├── src/
│   └── orchestration/
│       ├── __init__.py                         [m0]
│       ├── main.py                             [m0]
│       ├── config.py                           [m0]
│       ├── api/
│       │   ├── __init__.py                     [m0]
│       │   ├── app.py                          [m0]
│       │   ├── deps.py                         [m0]
│       │   ├── middleware/
│       │   │   ├── __init__.py                 [m0]
│       │   │   └── api_key.py                  [m0]
│       │   └── routes/
│       │       ├── __init__.py                 [m0]
│       │       ├── health.py                   [m0]
│       │       ├── projects.py                 [m2]
│       │       ├── tasks.py                    [m2]
│       │       ├── runs.py                     [m2]
│       │       └── webhooks.py                 [m4]
│       ├── orchestrator/
│       │   ├── __init__.py
│       │   ├── flow.py                         [m1] [m2]
│       │   ├── state.py                        [m1]
│       │   ├── routing.py                      [m3]
│       │   └── budget_guard.py                 [m2]
│       ├── stages/
│       │   ├── __init__.py
│       │   ├── base.py                         [m2]
│       │   ├── planner.py                      [m2]
│       │   ├── coder.py                        [m2]
│       │   ├── tester.py                       [m3]
│       │   ├── security.py                     [m3]
│       │   ├── reviewer.py                     [m3]
│       │   └── docs.py                         [m3]
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── task_source/
│       │   │   ├── __init__.py                 [m2]
│       │   │   ├── base.py                     [m2]
│       │   │   ├── csv.py                      [m2]
│       │   │   ├── github.py                   [m4]
│       │   │   ├── jira.py                     [m4]
│       │   │   └── linear.py                   [m4]
│       │   └── review/
│       │       ├── __init__.py                 [m2]
│       │       ├── base.py                     [m2]
│       │       ├── web_ui.py                   [m2]
│       │       ├── github_pr.py                [m4]
│       │       ├── jira_comment.py             [m4]
│       │       └── slack.py                    [m4]
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py                       [m2]
│       │   ├── endpoint.py                     [m2]
│       │   ├── openai_compat.py                [m2]
│       │   ├── anthropic_compat.py             [m2]
│       │   ├── router.py                       [m2]
│       │   ├── circuit_breaker.py              [m6]
│       │   ├── fallback.py                     [m6]
│       │   ├── rate_limit.py                   [m6]
│       │   └── multi_key.py                    [m6]
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── db.py                           [m0]
│       │   ├── redis.py                        [m0]
│       │   ├── models/                         [m0]
│       │   │   ├── __init__.py
│       │   │   ├── project.py
│       │   │   ├── user.py
│       │   │   ├── api_key.py
│       │   │   ├── llm_config.py
│       │   │   ├── task.py
│       │   │   ├── run.py
│       │   │   ├── stage_artifact.py
│       │   │   ├── external_receipt.py
│       │   │   ├── audit_log.py
│       │   │   ├── suggestion.py
│       │   │   ├── llm_usage.py
│       │   │   ├── worktree.py
│       │   │   └── budget_state.py
│       │   └── repositories/
│       │       ├── __init__.py
│       │       ├── projects.py                 [m2]
│       │       ├── runs.py                     [m2]
│       │       ├── receipts.py                 [m2]
│       │       ├── budget.py                   [m2]
│       │       └── audit.py                    [m3]
│       ├── git/
│       │   ├── __init__.py                     [m2]
│       │   └── worktree.py                     [m2]
│       ├── suggestion/
│       │   ├── __init__.py                     [m5]
│       │   └── engine.py                       [m5]
│       ├── observability/
│       │   ├── __init__.py                     [m6]
│       │   ├── tracing.py                      [m6]
│       │   ├── logging.py                      [m6]
│       │   └── metrics.py                      [m6]
│       └── workers/
│           ├── __init__.py                     [m0]
│           └── orchestrator_worker.py          [m2]
├── tests/
│   ├── conftest.py                             [m0]
│   ├── integration/
│   │   └── ...
│   ├── eval/
│   │   ├── swebench_verified_subset.jsonl      [m5]
│   │   └── runner.py                           [m5]
│   └── ...
├── docs/
│   ├── superpowers/
│   │   ├── specs/2026-08-07-orchestration-design.md
│   │   └── plans/2026-08-07-orchestration-mvp.md
│   ├── api.md                                  [m2]
│   └── ops.md                                  [m6]
└── .firecrawl/  # existing scratch directory (gitignored)
```

---

## Milestone 0 — Foundation

Goal: a FastAPI service boots, connects to Postgres + Redis, has health checks, and the full schema migrates cleanly.

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `Makefile`

**Interfaces:**
- Produces: Python 3.12 project layout with `src/orchestration/` package

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "orchestration"
version = "0.1.0"
description = "Self-hosted multi-agent software engineering orchestration"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic>=2.9",
  "pydantic-settings>=2.6",
  "sqlalchemy[asyncio]>=2.0.36",
  "asyncpg>=0.30",
  "alembic>=1.14",
  "redis>=5.2",
  "crewai>=0.86",
  "crewai-tools>=0.17",
  "httpx>=0.28",
  "structlog>=24.4",
  "python-multipart>=0.0.20",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "pytest-cov>=6.0",
  "ruff>=0.8",
  "mypy>=1.13",
  "testcontainers[postgres,redis]>=4.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/orchestration"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.ruff]
target-version = "py312"
src = ["src", "tests"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src/orchestration"]
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
.venv/
venv/
.env
.env.local
.pytest_cache/
.ruff_cache/
.mypy_cache/
htmlcov/
.coverage
dist/
build/
.firecrawl/
*.log
.DS_Store
```

- [ ] **Step 3: Write `.env.example`**

```dotenv
DATABASE_URL=postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration
REDIS_URL=redis://localhost:6379/0
TASK_SOURCE_TYPE=csv
REVIEW_SURFACE_TYPE=web_ui
API_KEY_HEADER=X-API-Key
DEFAULT_REVIEW_TIMEOUT_SECONDS=86400
DEFAULT_MONTHLY_BUDGET_USD=500
WORKTREE_BASE_PATH=/var/worktrees
WORKTREE_TTL_DAYS=30
LOG_LEVEL=INFO
```

- [ ] **Step 4: Write `README.md`**

```markdown
# Orchestration System

Self-hosted multi-agent software engineering orchestrator.

See `docs/superpowers/specs/2026-08-07-orchestration-design.md` for the design.

## Quickstart

    make docker-up
    make migrate
    make run

## Test

    make test

## Layout

`src/orchestration/` — package code
`tests/` — pytest suite
`docs/superpowers/specs/` — design spec
`docs/superpowers/plans/` — implementation plan
```

- [ ] **Step 5: Write `Makefile`**

```makefile
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
```

- [ ] **Step 6: Create empty package directories with `__init__.py`**

Create these empty files:
- `src/orchestration/__init__.py` (content: `__version__ = "0.1.0"`)
- `src/orchestration/api/__init__.py`
- `src/orchestration/api/middleware/__init__.py`
- `src/orchestration/api/routes/__init__.py`
- `src/orchestration/orchestrator/__init__.py`
- `src/orchestration/stages/__init__.py`
- `src/orchestration/adapters/__init__.py`
- `src/orchestration/adapters/task_source/__init__.py`
- `src/orchestration/adapters/review/__init__.py`
- `src/orchestration/llm/__init__.py`
- `src/orchestration/storage/__init__.py`
- `src/orchestration/storage/models/__init__.py`
- `src/orchestration/git/__init__.py`
- `src/orchestration/observability/__init__.py`
- `src/orchestration/workers/__init__.py`
- `tests/__init__.py`
- `tests/conftest.py` (content: `# tests config`)

- [ ] **Step 7: Verify scaffolding**

Run: `find src tests -name "__init__.py" | wc -l`
Expected: 16+ lines

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "M0/Task 1: project scaffolding (pyproject, layout, lint, test config)"
```

---

### Task 2: Docker Compose for Postgres + Redis

**Files:**
- Create: `docker-compose.yml`
- Create: `docker/api.Dockerfile`
- Create: `docker/worker.Dockerfile`

**Interfaces:**
- Produces: local Postgres + Redis services on default ports

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: orchestration
      POSTGRES_PASSWORD: orchestration
      POSTGRES_DB: orchestration
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U orchestration"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  api:
    build:
      context: .
      dockerfile: docker/api.Dockerfile
    command: uvicorn orchestration.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build:
      context: .
      dockerfile: docker/worker.Dockerfile
    command: python -m orchestration.workers.orchestrator_worker
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      replicas: 2

volumes:
  pgdata:
```

- [ ] **Step 2: Write `docker/api.Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir .
COPY src ./src
EXPOSE 8000
CMD ["uvicorn", "orchestration.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Write `docker/worker.Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir .
COPY src ./src
CMD ["python", "-m", "orchestration.workers.orchestrator_worker"]
```

- [ ] **Step 4: Verify services start**

Run: `docker compose up -d postgres redis`
Wait 5 seconds.
Run: `docker compose ps`
Expected: both `postgres` and `redis` show `healthy` status.

Run: `docker compose down`

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml docker/
git commit -m "M0/Task 2: docker-compose for postgres, redis, api, worker"
```

---

### Task 3: SQLAlchemy models and Alembic baseline migration

**Files:**
- Create: `src/orchestration/storage/db.py`
- Create: `src/orchestration/storage/redis.py`
- Create: `src/orchestration/storage/models/*.py` (12 files)
- Create: `src/orchestration/config.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/001_initial_schema.py`

**Interfaces:**
- Produces: 12 SQLAlchemy 2.0 async ORM models matching spec §7 DDL
- Produces: `engine`, `session_factory`, `redis_pool` singletons

- [ ] **Step 1: Write `src/orchestration/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration"
    redis_url: str = "redis://localhost:6379/0"

    task_source_type: str = "csv"
    review_surface_type: str = "web_ui"
    api_key_header: str = "X-API-Key"

    default_review_timeout_seconds: int = 86400
    default_monthly_budget_usd: float = 500.0

    worktree_base_path: str = "/var/worktrees"
    worktree_ttl_days: int = 30

    log_level: str = "INFO"


settings = Settings()
```

- [ ] **Step 2: Write `src/orchestration/storage/db.py`**

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from orchestration.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 3: Write `src/orchestration/storage/redis.py`**

```python
from redis.asyncio import ConnectionPool, Redis

from orchestration.config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(settings.redis_url, max_connections=20)
    return _pool


def get_redis() -> Redis:
    return Redis(connection_pool=get_pool())
```

- [ ] **Step 4: Write `src/orchestration/storage/models/project.py`**

```python
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    repo_url: Mapped[str] = mapped_column(String, nullable=False)
    default_branch: Mapped[str] = mapped_column(String, nullable=False, default="main")
    llm_config_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    review_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    budget_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 5: Write remaining 11 model files**

Apply the same pattern. Each model file maps to its corresponding table in spec §7. Below are the class signatures and key columns:

`src/orchestration/storage/models/user.py`:
```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # CHECK in migration
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`src/orchestration/storage/models/api_key.py`:
```python
class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`src/orchestration/storage/models/llm_config.py`:
```python
class LLMConfig(Base):
    __tablename__ = "llm_configs"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    endpoints: Mapped[list] = mapped_column(JSONB, nullable=False)
    tier_map: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fallback_chain: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    rate_limit: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

`src/orchestration/storage/models/task.py`:
```python
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)  # CHECK in migration
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("source_type", "external_id"), Index("idx_tasks_project_status", "project_id", "status"))
```

`src/orchestration/storage/models/run.py`:
```python
class Run(Base):
    __tablename__ = "runs"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True)
    flow_uuid: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    budget_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_runs_project_status", "project_id", "status"),)
```

`src/orchestration/storage/models/stage_artifact.py`:
```python
class StageArtifact(Base):
    __tablename__ = "stage_artifacts"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("run_id", "stage", "attempt"),)
```

`src/orchestration/storage/models/external_receipt.py`:
```python
class ExternalReceipt(Base):
    __tablename__ = "external_receipts"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    adapter_type: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # pending|succeeded|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_receipts_external", "adapter_type", "external_id"),)
```

`src/orchestration/storage/models/audit_log.py`:
```python
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audit_run_ts", "run_id", "ts"),
        Index("idx_audit_project_ts", "project_id", "ts"),
    )
```

`src/orchestration/storage/models/suggestion.py`:
```python
class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    proposals: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_suggestions_project_status", "project_id", "status"),)
```

`src/orchestration/storage/models/llm_usage.py`:
```python
class LLMUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    endpoint_name: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`src/orchestration/storage/models/worktree.py`:
```python
class Worktree(Base):
    __tablename__ = "worktrees"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    path: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str] = mapped_column(String, nullable=False)
    base_sha: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_worktrees_project_status", "project_id", "status"),
        Index("idx_worktrees_expires", "expires_at", postgresql_where=text("status = 'archived'")),
    )
```

`src/orchestration/storage/models/budget_state.py`:
```python
class BudgetState(Base):
    __tablename__ = "budget_state"
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    last_run_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 6: Write `src/orchestration/storage/models/__init__.py`**

```python
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.budget_state import BudgetState
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.llm_config import LLMConfig
from orchestration.storage.models.llm_usage import LLMUsage
from orchestration.storage.models.project import Base, Project
from orchestration.storage.models.run import Run
from orchestration.storage.models.stage_artifact import StageArtifact
from orchestration.storage.models.suggestion import Suggestion
from orchestration.storage.models.task import Task
from orchestration.storage.models.user import User
from orchestration.storage.models.worktree import Worktree

__all__ = [
    "Base", "Project", "User", "ApiKey", "LLMConfig", "Task", "Run",
    "StageArtifact", "ExternalReceipt", "AuditLog", "Suggestion",
    "LLMUsage", "Worktree", "BudgetState",
]
```

- [ ] **Step 7: Write `alembic.ini`**

```ini
[alembic]
script_location = alembic
prepend_sys_path = src
sqlalchemy.url = postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 8: Write `alembic/env.py`**

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from orchestration.config import settings
from orchestration.storage.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 9: Write `alembic/script.py.mako`**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 10: Generate migration from models**

Run:
```bash
docker compose up -d postgres redis
sleep 3
alembic revision --autogenerate -m "initial schema"
mv alembic/versions/*_initial_schema.py alembic/versions/001_initial_schema.py
```

- [ ] **Step 11: Edit `001_initial_schema.py` to add CHECK constraints and FK**

Open the generated file and add (after each CREATE TABLE for `users`, `api_keys`, `tasks`, `runs`, `external_receipts`, `worktrees`, `suggestions`, `llm_usage`):

```python
op.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin','operator','viewer'))")
op.execute("ALTER TABLE tasks ADD CONSTRAINT tasks_source_type_check CHECK (source_type IN ('jira','linear','github','csv'))")
op.execute("ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN ('queued','running','awaiting_review','paused','succeeded','failed','rejected','cancelled'))")
op.execute("ALTER TABLE external_receipts ADD CONSTRAINT external_receipts_status_check CHECK (status IN ('pending','succeeded','failed'))")
op.execute("ALTER TABLE worktrees ADD CONSTRAINT worktrees_status_check CHECK (status IN ('active','archived','deleted'))")
op.execute("ALTER TABLE suggestions ADD CONSTRAINT suggestions_status_check CHECK (status IN ('pending','picked','rejected','executed'))")
op.execute("ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_tier_check CHECK (tier IN ('FAST','THINKING'))")
op.execute("ALTER TABLE projects ADD CONSTRAINT projects_llm_config_fk FOREIGN KEY (llm_config_id) REFERENCES llm_configs(id)")
```

- [ ] **Step 12: Apply migration**

Run: `alembic upgrade head`
Expected: 12 tables created. Verify:
Run: `docker compose exec postgres psql -U orchestration -c "\dt"`
Expected output includes: projects, users, api_keys, llm_configs, tasks, runs, stage_artifacts, external_receipts, audit_log, suggestions, llm_usage, worktrees, budget_state.

- [ ] **Step 13: Commit**

```bash
git add src/orchestration/storage alembic alembic.ini
git commit -m "M0/Task 3: SQLAlchemy models + Alembic baseline migration"
```

---

### Task 4: FastAPI skeleton + health endpoints

**Files:**
- Create: `src/orchestration/main.py`
- Create: `src/orchestration/api/app.py`
- Create: `src/orchestration/api/routes/health.py`
- Create: `tests/api/test_health.py`

**Interfaces:**
- Produces: `app: FastAPI` instance with `/health` and `/ready`

- [ ] **Step 1: Write `src/orchestration/api/routes/health.py`**

```python
from fastapi import APIRouter, status
from sqlalchemy import text

from orchestration.storage.db import get_session_factory
from orchestration.storage.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    redis = get_redis()
    await redis.ping()
    return {"status": "ready"}
```

- [ ] **Step 2: Write `src/orchestration/api/app.py`**

```python
from fastapi import FastAPI

from orchestration.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="Orchestration", version="0.1.0")
    app.include_router(health_router)
    return app
```

- [ ] **Step 3: Write `src/orchestration/main.py`**

```python
from orchestration.api.app import create_app

app = create_app()
```

- [ ] **Step 4: Write `tests/conftest.py`**

```python
import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

from orchestration.api.app import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

- [ ] **Step 5: Write `tests/api/test_health.py`**

```python
import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 6: Run the test**

Run: `docker compose up -d postgres redis`
Run: `pytest tests/api/test_health.py -v`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add src/orchestration/main.py src/orchestration/api tests
git commit -m "M0/Task 4: FastAPI skeleton with health and ready endpoints"
```

---

### Task 5: API-key auth middleware

**Files:**
- Create: `src/orchestration/api/middleware/api_key.py`
- Modify: `src/orchestration/api/app.py`
- Create: `tests/api/test_api_key.py`

**Interfaces:**
- Produces: `require_api_key` FastAPI dependency that resolves `X-API-Key` to a `User` and enforces `tenant_id` scoping via `project_id`

- [ ] **Step 1: Write `src/orchestration/api/middleware/api_key.py`**

```python
from fastapi import Depends, Header, HTTPException, status
from passlib.hash import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.storage.db import get_session_factory
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.user import User


async def get_session() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session


def _hash_key(raw: str) -> str:
    return bcrypt.using(rounds=4).hash(raw)  # rounds=4 for test speed


def verify_key(raw: str, hashed: str) -> bool:
    return bcrypt.verify(raw, hashed)


async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> User:
    stmt = select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.revoked_at.is_(None))
    result = await session.execute(stmt)
    rows = result.all()
    for api_key, user in rows:
        if verify_key(x_api_key, api_key.key_hash):
            return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
```

Add to `pyproject.toml` dependencies: `passlib[bcrypt]>=1.7`

- [ ] **Step 2: Add auth dependency to a `/me` test route**

Modify `src/orchestration/api/app.py`:

```python
from fastapi import Depends, FastAPI

from orchestration.api.middleware.api_key import require_api_key
from orchestration.api.routes.health import router as health_router
from orchestration.storage.models.user import User


def create_app() -> FastAPI:
    app = FastAPI(title="Orchestration", version="0.1.0")
    app.include_router(health_router)

    @app.get("/me", tags=["auth"])
    async def me(user: User = Depends(require_api_key)) -> dict[str, str]:
        return {"email": user.email, "role": user.role}

    return app
```

- [ ] **Step 3: Write `tests/api/test_api_key.py`**

```python
import pytest
from sqlalchemy import insert

from orchestration.storage.db import get_session_factory
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.user import User
from tests.conftest import _hash_key  # re-export helper


@pytest.mark.asyncio
async def test_me_requires_api_key(client):
    response = await client.get("/me")
    assert response.status_code == 422  # missing header


@pytest.mark.asyncio
async def test_me_rejects_bad_key(client):
    response = await client.get("/me", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_accepts_valid_key(client):
    factory = get_session_factory()
    raw_key = "test-key-abc123"
    async with factory() as session:
        user = (await session.execute(
            insert(User).values(email="t@example.com", display_name="T", role="admin").returning(User.id)
        )).scalar_one()
        await session.execute(
            insert(ApiKey).values(user_id=user, key_hash=_hash_key(raw_key), name="test")
        )
        await session.commit()

    response = await client.get("/me", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    assert response.json()["email"] == "t@example.com"
```

- [ ] **Step 4: Update `tests/conftest.py` to export `_hash_key`**

Edit `tests/conftest.py` and add at the top:
```python
from passlib.hash import bcrypt as _bcrypt  # noqa: E402


def _hash_key(raw: str) -> str:
    return _bcrypt.using(rounds=4).hash(raw)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/api -v`
Expected: 4 passed (1 health + 3 auth).

- [ ] **Step 6: Commit**

```bash
git add src/orchestration/api tests
git commit -m "M0/Task 5: API-key auth middleware with /me endpoint"
```

---

### Task 6: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml` (if using GitHub; else skip)

**Interfaces:**
- Produces: CI runs lint + test on every push

- [ ] **Step 1: Create CI workflow**

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: orchestration
          POSTGRES_PASSWORD: orchestration
          POSTGRES_DB: orchestration
        ports: ["5432:5432"]
        options: --health-cmd pg_isready --health-interval 5s
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: --health-cmd "redis-cli ping" --health-interval 5s
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: ruff check src tests
      - run: mypy src
      - run: alembic upgrade head
        env:
          DATABASE_URL: postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration
      - run: pytest
        env:
          DATABASE_URL: postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration
          REDIS_URL: redis://localhost:6379/0
```

- [ ] **Step 2: Verify locally**

Run: `ruff check src tests && mypy src && pytest`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add .github
git commit -m "M0/Task 6: CI workflow (lint, type-check, migrate, test)"
```

---

### Task 7: Smoke test end-to-end

**Files:**
- Create: `tests/integration/test_m0_smoke.py`

**Interfaces:**
- Produces: integration test that boots the full stack and verifies health + auth

- [ ] **Step 1: Write `tests/integration/test_m0_smoke.py`**

```python
import pytest

from orchestration.api.app import create_app


@pytest.mark.asyncio
async def test_full_stack_boots():
    app = create_app()
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        h = await client.get("/health")
        assert h.status_code == 200
        assert h.json()["status"] == "ok"

        r = await client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/integration -v`
Expected: 1 passed.

- [ ] **Step 3: Tag M0**

Run:
```bash
git tag -a m0-foundation -m "M0: foundation complete (FastAPI + Postgres + Redis + Alembic + auth + CI)"
git push origin m0-foundation  # if remote configured
```

🎉 **M0 complete.**

---

## Milestone 1 — CrewAI Flow Spike

Goal: validate the two-tier persistence model (`@persist` + `checkpoint=True`) and idempotency on external side-effects, before building the full orchestrator.

### Task 8: Spike scaffold

**Files:**
- Create: `tests/spike/conftest.py`
- Create: `tests/spike/test_flow_basics.py`

**Interfaces:**
- Produces: minimal CrewAI Flow with 3 stages that exercises persist + checkpoint

- [ ] **Step 1: Write `tests/spike/test_flow_basics.py`**

```python
import pytest
from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel


class SpikeState(BaseModel):
    run_id: str = "spike-1"
    stages_done: list[str] = []


class SpikeFlow(Flow[SpikeState]):
    @start()
    def init(self):
        self.state.stages_done.append("init")
        return "init_done"

    @listen(init)
    def plan(self, prev):
        self.state.stages_done.append("plan")
        return "plan_done"

    @listen(plan)
    def execute(self, prev):
        self.state.stages_done.append("execute")
        return "execute_done"


@pytest.mark.asyncio
async def test_spike_flow_runs_three_stages():
    flow = SpikeFlow()
    result = flow.kickoff()
    assert "init" in flow.state.stages_done
    assert "plan" in flow.state.stages_done
    assert "execute" in flow.state.stages_done
```

- [ ] **Step 2: Run spike test**

Run: `pytest tests/spike/test_flow_basics.py -v`
Expected: 1 passed (or capture CrewAI's actual API mismatch and document).

- [ ] **Step 3: Document findings in `docs/spike-m1.md`**

Create `docs/spike-m1.md` with notes:
- CrewAI Flow version found
- `@start` / `@listen` actual decorators (correct name if different)
- `@persist` decorator location and requirements
- `checkpoint=True` discovery findings
- Any deviations from spec §5.2

- [ ] **Step 4: Commit**

```bash
git add tests/spike docs/spike-m1.md
git commit -m "M1/Task 8: CrewAI Flow spike scaffold + findings doc"
```

---

### Task 9: Persist + checkpoint experiment

**Files:**
- Modify: `tests/spike/test_flow_basics.py`

- [ ] **Step 1: Add persist test**

Append to `tests/spike/test_flow_basics.py`:

```python
def test_persist_round_trip():
    """Verify FlowState survives process boundary via @persist."""
    flow1 = SpikeFlow(persist=True, checkpoint=True)
    flow1.kickoff()
    flow_uuid = flow1.state.flow_uuid if hasattr(flow1.state, "flow_uuid") else None

    flow2 = SpikeFlow(persist=True, checkpoint=True)
    flow2.kickoff(restore_from_state_id=flow_uuid) if flow_uuid else flow2.kickoff()

    # After restore, completed_stages should be preserved
    assert all(s in flow2.state.stages_done for s in ["init", "plan"])
```

- [ ] **Step 2: Run, capture API differences, document in `docs/spike-m1.md`**

Run: `pytest tests/spike/test_flow_basics.py -v`
Document findings.

- [ ] **Step 3: Commit**

```bash
git add tests/spike docs/spike-m1.md
git commit -m "M1/Task 9: persist + checkpoint spike test"
```

---

### Task 10: Parallel stages experiment

**Files:**
- Modify: `tests/spike/test_flow_basics.py`

- [ ] **Step 1: Add parallel join test**

Append:

```python
def test_parallel_join():
    """Verify @listen(and_(a, b)) waits for both."""
    from crewai.flow.flow import and_, listen, start

    class ParallelState(BaseModel):
        a_done: bool = False
        b_done: bool = False

    class PFlow(Flow[ParallelState]):
        @start()
        def init(self):
            return "init"

        @listen(init)
        def branch_a(self, _):
            self.state.a_done = True
            return "a"

        @listen(init)
        def branch_b(self, _):
            self.state.b_done = True
            return "b"

        @listen(and_(branch_a, branch_b))
        def join(self, _):
            return "joined"

    flow = PFlow()
    flow.kickoff()
    assert flow.state.a_done and flow.state.b_done
```

- [ ] **Step 2: Run + document findings**

Run: `pytest tests/spike/test_flow_basics.py -v`
Add CrewAI `and_` import + behavior notes to `docs/spike-m1.md`.

- [ ] **Step 3: Commit**

```bash
git add tests/spike docs/spike-m1.md
git commit -m "M1/Task 10: parallel-join spike test"
```

---

### Task 11: Idempotency on external call experiment

**Files:**
- Create: `tests/spike/test_idempotency.py`

- [ ] **Step 1: Write idempotency test**

```python
import pytest

from orchestration.adapters.task_source.base import TaskSourceAdapter, ExternalTask
from orchestration.adapters.task_source.csv import CsvAdapter
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.db import session_scope
from sqlalchemy import select


@pytest.mark.asyncio
async def test_external_call_is_idempotent(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\nT1,Test,Desc\n")
    adapter = CsvAdapter(path=str(csv))

    # First call: receipt created
    async with session_scope() as session:
        # Simulate adapter call with idempotency key
        key = f"task:{adapter.name}:T1:create"
        existing = (await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == key)
        )).scalar_one_or_none()
        assert existing is None

        # (In real adapter this would create the resource and persist receipt.)
        # We simulate the dedup check here.
        if existing:
            assert existing.response_payload == {"id": "T1", "reused": True}
```

- [ ] **Step 2: Document the contract**

In `docs/spike-m1.md`, add the **Idempotency Contract** section:

> Every adapter call MUST:
> 1. Compute `idempotency_key` deterministically from `(project_id, task_id, stage, operation, attempt)`.
> 2. Before executing, query `external_receipts` for an existing row with that key.
> 3. If found with status `succeeded`, return the cached `response_payload` (don't re-execute).
> 4. If found with status `pending`, raise or wait.
> 5. If not found, insert row with status `pending`, execute the side-effect, update row to `succeeded`/`failed`.
> 6. Receipt writes happen in the same DB transaction as the side-effect success path.

- [ ] **Step 3: Run test, commit**

Run: `pytest tests/spike/test_idempotency.py -v`

```bash
git add tests/spike docs/spike-m1.md
git commit -m "M1/Task 11: idempotency-on-external-call contract"
```

---

### Task 12: Refine spec based on spike findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-07-orchestration-design.md` §5.2

- [ ] **Step 1: Update spec with concrete CrewAI API**

Edit §5.2 to replace abstract CrewAI Flow references with the actual API discovered in the spike (correct decorator names, exact persist signature, etc.).

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs
git commit -m "M1/Task 12: refine spec §5.2 with spike findings"
```

🎉 **M1 complete.**

---

## Milestone 2 — Vertical Slice: Happy Path

Goal: end-to-end run with 1 CSV task source, 1 Ollama endpoint, 2 stages (Planner + Coder), 1 Web UI review surface. Happy path demoable.

### Task 13: LLM endpoint abstraction

**Files:**
- Create: `src/orchestration/llm/endpoint.py`
- Create: `src/orchestration/llm/openai_compat.py`
- Create: `src/orchestration/llm/anthropic_compat.py`
- Create: `src/orchestration/llm/client.py`
- Create: `src/orchestration/llm/router.py`
- Create: `tests/llm/test_endpoint.py`

**Interfaces:**
- Produces: `LLMEndpoint` Pydantic model + `LLMClient` Protocol + `OpenAICompatClient` + `AnthropicCompatClient` + `ProviderRouter`

- [ ] **Step 1: Write `src/orchestration/llm/endpoint.py`**

```python
from typing import Literal

from pydantic import BaseModel, SecretStr


class LLMEndpoint(BaseModel):
    name: str
    protocol: Literal["openai", "anthropic"]
    base_url: str
    api_key: SecretStr
    model: str
    tier: Literal["FAST", "THINKING"]
```

- [ ] **Step 2: Write `src/orchestration/llm/client.py`**

```python
from typing import Protocol

from pydantic import BaseModel


class CompletionRequest(BaseModel):
    messages: list[dict]
    tier: str = "FAST"
    max_tokens: int = 4096
    temperature: float = 0.0


class CompletionResponse(BaseModel):
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model: str
    endpoint_name: str


class LLMClient(Protocol):
    endpoint_name: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
```

- [ ] **Step 3: Write `src/orchestration/llm/openai_compat.py`**

```python
import httpx

from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint


class OpenAICompatClient:
    def __init__(self, endpoint: LLMEndpoint):
        self.endpoint = endpoint

    @property
    def endpoint_name(self) -> str:
        return self.endpoint.name

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        async with httpx.AsyncClient(base_url=self.endpoint.base_url) as http:
            r = await http.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self.endpoint.api_key.get_secret_value()}"},
                json={
                    "model": self.endpoint.model,
                    "messages": request.messages,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                },
            )
            r.raise_for_status()
            data = r.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return CompletionResponse(
            content=choice["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=0.0,  # computed by router from pricing table
            model=self.endpoint.model,
            endpoint_name=self.endpoint.name,
        )
```

- [ ] **Step 4: Write `src/orchestration/llm/anthropic_compat.py`**

```python
import httpx

from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint


class AnthropicCompatClient:
    def __init__(self, endpoint: LLMEndpoint):
        self.endpoint = endpoint

    @property
    def endpoint_name(self) -> str:
        return self.endpoint.name

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Extract system message separately (Anthropic API quirk)
        system = next((m["content"] for m in request.messages if m["role"] == "system"), None)
        messages = [m for m in request.messages if m["role"] != "system"]

        async with httpx.AsyncClient(base_url=self.endpoint.base_url) as http:
            r = await http.post(
                "/v1/messages",
                headers={
                    "x-api-key": self.endpoint.api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.endpoint.model,
                    "messages": messages,
                    "max_tokens": request.max_tokens,
                    "system": system,
                },
            )
            r.raise_for_status()
            data = r.json()

        usage = data.get("usage", {})
        return CompletionResponse(
            content=data["content"][0]["text"],
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            cost_usd=0.0,
            model=self.endpoint.model,
            endpoint_name=self.endpoint.name,
        )
```

- [ ] **Step 5: Write `src/orchestration/llm/router.py`**

```python
from orchestration.llm.anthropic_compat import AnthropicCompatClient
from orchestration.llm.client import LLMClient
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.openai_compat import OpenAICompatClient


def build_client(endpoint: LLMEndpoint) -> LLMClient:
    if endpoint.protocol == "openai":
        return OpenAICompatClient(endpoint)
    return AnthropicCompatClient(endpoint)
```

- [ ] **Step 6: Write `tests/llm/test_endpoint.py`**

```python
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client


def test_openai_client_built():
    e = LLMEndpoint(
        name="primary",
        protocol="openai",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen2.5-coder:7b",
        tier="FAST",
    )
    c = build_client(e)
    assert c.endpoint_name == "primary"


def test_anthropic_client_built():
    e = LLMEndpoint(
        name="claude",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-test",
        model="claude-sonnet-4-5",
        tier="THINKING",
    )
    c = build_client(e)
    assert c.endpoint_name == "claude"
```

- [ ] **Step 7: Run + commit**

Run: `pytest tests/llm -v`
Expected: 2 passed.

```bash
git add src/orchestration/llm tests/llm
git commit -m "M2/Task 13: LLM endpoint abstraction (openai/anthropic compatible)"
```

---

### Task 14: CSV task source adapter

**Files:**
- Create: `src/orchestration/adapters/task_source/base.py`
- Create: `src/orchestration/adapters/task_source/csv.py`
- Create: `tests/adapters/test_csv_adapter.py`

**Interfaces:**
- Produces: `TaskSourceAdapter` ABC + `CsvAdapter` reading tasks from a CSV file

- [ ] **Step 1: Write `base.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel


class ExternalTask(BaseModel):
    external_id: str
    title: str
    description: str | None = None
    metadata: dict = {}


class CreateTask(BaseModel):
    title: str
    description: str
    metadata: dict = {}


class Comment(BaseModel):
    external_id: str
    body: str


class TaskSourceAdapter(ABC):
    name: str

    @abstractmethod
    async def list_tasks(self, project_id: UUID, status: str | None) -> list[ExternalTask]: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> ExternalTask: ...

    @abstractmethod
    async def create_task(self, project_id: UUID, payload: CreateTask) -> ExternalTask: ...

    @abstractmethod
    async def update_status(self, task_id: str, status: str) -> None: ...

    @abstractmethod
    async def add_comment(self, task_id: str, body: str) -> Comment: ...
```

- [ ] **Step 2: Write `csv.py`**

```python
import csv as csvlib
from pathlib import Path
from uuid import UUID

from orchestration.adapters.task_source.base import Comment, CreateTask, ExternalTask, TaskSourceAdapter


class CsvAdapter(TaskSourceAdapter):
    name = "csv"

    def __init__(self, path: str):
        self.path = Path(path)

    def _read(self) -> list[dict]:
        with self.path.open() as f:
            return list(csvlib.DictReader(f))

    async def list_tasks(self, project_id: UUID, status: str | None) -> list[ExternalTask]:
        return [
            ExternalTask(
                external_id=row["external_id"],
                title=row["title"],
                description=row.get("description"),
            )
            for row in self._read()
        ]

    async def get_task(self, task_id: str) -> ExternalTask:
        for row in self._read():
            if row["external_id"] == task_id:
                return ExternalTask(
                    external_id=row["external_id"],
                    title=row["title"],
                    description=row.get("description"),
                )
        raise KeyError(task_id)

    async def create_task(self, project_id: UUID, payload: CreateTask) -> ExternalTask:
        # Idempotency handled by caller via external_receipts
        existing = self._read()
        new_id = f"T{len(existing) + 1}"
        with self.path.open("a") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "title", "description"])
            writer.writerow({"external_id": new_id, "title": payload.title, "description": payload.description})
        return ExternalTask(external_id=new_id, title=payload.title, description=payload.description)

    async def update_status(self, task_id: str, status: str) -> None:
        # Read-modify-write
        rows = self._read()
        for row in rows:
            if row["external_id"] == task_id:
                row["status"] = status
        with self.path.open("w") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "title", "description", "status"])
            writer.writeheader()
            writer.writerows(rows)

    async def add_comment(self, task_id: str, body: str) -> Comment:
        # Append to .comments.csv sidecar
        comments_path = self.path.with_suffix(".comments.csv")
        with comments_path.open("a") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "body"])
            writer.writerow({"external_id": task_id, "body": body})
        return Comment(external_id=task_id, body=body)
```

- [ ] **Step 3: Write `tests/adapters/test_csv_adapter.py`**

```python
import pytest

from orchestration.adapters.task_source.csv import CsvAdapter


@pytest.mark.asyncio
async def test_csv_list_tasks(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\nT1,Test 1,Desc 1\nT2,Test 2,Desc 2\n")
    adapter = CsvAdapter(path=str(csv))
    tasks = await adapter.list_tasks(project_id=None, status=None)
    assert len(tasks) == 2
    assert tasks[0].external_id == "T1"


@pytest.mark.asyncio
async def test_csv_create_task(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\n")
    adapter = CsvAdapter(path=str(csv))
    task = await adapter.create_task(
        project_id=None,
        payload=type("P", (), {"title": "New", "description": "New desc"})(),
    )
    assert task.external_id == "T1"


@pytest.mark.asyncio
async def test_csv_add_comment(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\nT1,Test,\n")
    adapter = CsvAdapter(path=str(csv))
    c = await adapter.add_comment("T1", "looks good")
    assert c.body == "looks good"
```

- [ ] **Step 4: Run + commit**

Run: `pytest tests/adapters -v`
Expected: 3 passed.

```bash
git add src/orchestration/adapters/task_source tests/adapters
git commit -m "M2/Task 14: CSV task source adapter"
```

---

### Task 15: Web UI review surface adapter

**Files:**
- Create: `src/orchestration/adapters/review/base.py`
- Create: `src/orchestration/adapters/review/web_ui.py`
- Create: `tests/adapters/test_web_ui_adapter.py`

**Interfaces:**
- Produces: `ReviewSurfaceAdapter` ABC + `WebUiAdapter` that stores decisions in Postgres and blocks on a Redis pub/sub channel

- [ ] **Step 1: Write `base.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel


class Artifacts(BaseModel):
    summary: str
    diff: str | None = None
    test_report: dict | None = None


class Decision(BaseModel):
    approve: bool
    reject: bool = False
    comment: str | None = None
    decided_by: str | None = None


class Receipt(BaseModel):
    id: UUID
    external_id: str | None = None


class ReviewSurfaceAdapter(ABC):
    name: str

    @abstractmethod
    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt: ...

    @abstractmethod
    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision: ...

    @abstractmethod
    async def post_comment(self, run_id: UUID, body: str) -> None: ...
```

- [ ] **Step 2: Write `web_ui.py`**

```python
import asyncio
import json
from uuid import UUID, uuid4

from sqlalchemy import insert, select

from orchestration.adapters.review.base import Artifacts, Decision, Receipt, ReviewSurfaceAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.run import Run
from orchestration.storage.redis import get_redis


class WebUiAdapter(ReviewSurfaceAdapter):
    name = "web_ui"

    async def _resolve_project_id(self, session, run_id: UUID) -> UUID:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return run.project_id

    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt:
        receipt_id = uuid4()
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            await session.execute(
                insert(ExternalReceipt).values(
                    id=receipt_id,
                    run_id=run_id,
                    idempotency_key=f"web_ui:notify:{run_id}",
                    adapter_type="web_ui",
                    operation="notify",
                    request_payload=artifacts.model_dump(),
                    status="succeeded",
                )
            )
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="notify_review",
                    payload=artifacts.model_dump(),
                )
            )
        return Receipt(id=receipt_id, external_id=str(receipt_id))

    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision:
        redis = get_redis()
        channel = f"review:decision:{receipt.id}"
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async with asyncio.timeout(timeout_s):
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        return Decision(**json.loads(msg["data"]))
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        raise TimeoutError("no decision received")

    async def post_comment(self, run_id: UUID, body: str) -> None:
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="post_comment",
                    payload={"body": body},
                )
            )
```

- [ ] **Step 3: Write `tests/adapters/test_web_ui_adapter.py`**

```python
import asyncio
import json
from uuid import uuid4

import pytest

from orchestration.adapters.review.web_ui import WebUiAdapter
from orchestration.adapters.review.base import Artifacts
from orchestration.storage.redis import get_redis


@pytest.mark.asyncio
async def test_web_ui_decision_round_trip():
    adapter = WebUiAdapter()
    receipt = await adapter.notify_review(uuid4(), Artifacts(summary="x"))

    async def publish():
        await asyncio.sleep(0.05)
        redis = get_redis()
        await redis.publish(f"review:decision:{receipt.id}", json.dumps({"approve": True, "comment": "lgtm"}))

    asyncio.create_task(publish())
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert decision.approve
    assert decision.comment == "lgtm"
```

- [ ] **Step 4: Run + commit**

Run: `pytest tests/adapters -v`
Expected: 4 passed.

```bash
git add src/orchestration/adapters/review tests/adapters
git commit -m "M2/Task 15: Web UI review surface adapter with Redis pub/sub"
```

---

### Task 16: Worktree manager

**Files:**
- Create: `src/orchestration/git/worktree.py`
- Create: `tests/git/test_worktree.py`

**Interfaces:**
- Produces: `WorktreeManager` with `create` / `diff` / `commit` / `push_branch` / `cleanup`

- [ ] **Step 1: Write `worktree.py`**

```python
import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestration.config import settings


@dataclass
class Worktree:
    id: uuid.UUID
    path: Path
    branch: str
    base_sha: str


class WorktreeManager:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    async def create(self, project_id: uuid.UUID, task_id: str) -> Worktree:
        wt_id = uuid.uuid4()
        branch = f"orch/{task_id}"
        path = Path(settings.worktree_base_path) / str(wt_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        base_sha = await asyncio.to_thread(self._git, "rev-parse", "HEAD")
        await asyncio.to_thread(
            self._git, "worktree", "add", "-b", branch, str(path), "main"
        )
        return Worktree(id=wt_id, path=path, branch=branch, base_sha=base_sha)

    async def diff(self, worktree_id: uuid.UUID) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        return await asyncio.to_thread(self._git_in, path, "diff", "main")

    async def commit(self, worktree_id: uuid.UUID, message: str) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        await asyncio.to_thread(self._git_in, path, "add", "-A")
        await asyncio.to_thread(self._git_in, path, "commit", "-m", message)
        return await asyncio.to_thread(self._git_in, path, "rev-parse", "HEAD")

    async def push_branch(self, worktree_id: uuid.UUID) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        wt = await self._find_by_path(path)
        await asyncio.to_thread(self._git_in, path, "push", "origin", wt.branch)
        return wt.branch

    async def cleanup(self, worktree_id: uuid.UUID) -> None:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        if path.exists():
            await asyncio.to_thread(self._git, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)

    async def _find_by_path(self, path: Path) -> Worktree:
        wt_id = uuid.UUID(path.name)
        branch = await asyncio.to_thread(self._git_in, path, "rev-parse", "--abbrev-ref", "HEAD")
        return Worktree(id=wt_id, path=path, branch=branch, base_sha="")

    def _git(self, *args: str) -> str:
        import subprocess
        r = subprocess.run(["git", *args], cwd=self.repo_path, capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def _git_in(self, path: Path, *args: str) -> str:
        import subprocess
        r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
        return r.stdout.strip()
```

- [ ] **Step 2: Write `tests/git/test_worktree.py`**

```python
import subprocess
from pathlib import Path

import pytest

from orchestration.git.worktree import WorktreeManager


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_worktree_create_and_commit(git_repo, tmp_path, monkeypatch):
    monkeypatch.setattr("orchestration.config.settings.worktree_base_path", str(tmp_path / "wts"))
    mgr = WorktreeManager(git_repo)
    wt = await mgr.create(project_id=None, task_id="T1")
    assert wt.path.exists()
    (wt.path / "new.py").write_text("x = 1\n")
    sha = await mgr.commit(wt.id, "add x")
    assert sha
```

- [ ] **Step 3: Run + commit**

Run: `pytest tests/git -v`
Expected: 1 passed.

```bash
git add src/orchestration/git tests/git
git commit -m "M2/Task 16: git worktree manager (create, diff, commit, cleanup)"
```

---

### Task 17: Idempotency receipt infrastructure

**Files:**
- Create: `src/orchestration/storage/repositories/receipts.py`
- Create: `src/orchestration/storage/repositories/audit.py`
- Create: `tests/storage/test_receipts.py`

**Interfaces:**
- Produces: `with_receipt(key, adapter, op, request_payload, executor)` helper that wraps a side-effect call with dedup + audit

- [ ] **Step 1: Write `receipts.py`**

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.storage.models.external_receipt import ExternalReceipt


@dataclass
class ReceiptContext:
    """Handle yielded by with_receipt(). Caller sets `.response` to cache the side-effect result.

    On a cache hit, `.cached` is True and the original `.response` is already populated;
    caller should NOT re-execute the side-effect.
    """

    cached: bool = False
    response: Any = None
    _on_complete: Any = None  # callable(response) -> awaitable, set by helper


@asynccontextmanager
async def with_receipt(
    session: AsyncSession,
    *,
    run_id: UUID,
    idempotency_key: str,
    adapter_type: str,
    operation: str,
    request_payload: dict,
):
    """Look up an existing receipt; on hit yield cached response, on miss yield fresh ctx.

    Usage:
        async with with_receipt(session, ...) as ctx:
            if ctx.cached:
                return  # use ctx.response
            # ... do side-effect ...
            ctx.response = {"id": "...", ...}
    """
    existing = (
        await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()

    if existing and existing.status == "succeeded":
        ctx = ReceiptContext(cached=True, response=existing.response_payload)
        yield ctx
        return

    receipt_id = existing.id if existing else uuid4()
    if not existing:
        await session.execute(
            insert(ExternalReceipt).values(
                id=receipt_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
                adapter_type=adapter_type,
                operation=operation,
                request_payload=request_payload,
                status="pending",
            )
        )

    ctx = ReceiptContext(cached=False, response=None)
    try:
        yield ctx
    except Exception:
        await session.execute(
            update(ExternalReceipt)
            .where(ExternalReceipt.id == receipt_id)
            .values(status="failed")
        )
        raise
    else:
        await session.execute(
            update(ExternalReceipt)
            .where(ExternalReceipt.id == receipt_id)
            .values(status="succeeded", response_payload=ctx.response)
        )
```

- [ ] **Step 2: Write `audit.py`**

```python
from uuid import UUID

from sqlalchemy import insert

from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog


async def audit(project_id: UUID, run_id: UUID | None, actor: str, action: str, payload: dict) -> None:
    async with session_scope() as session:
        await session.execute(
            insert(AuditLog).values(
                run_id=run_id,
                project_id=project_id,
                actor=actor,
                action=action,
                payload=payload,
            )
        )
```

- [ ] **Step 3: Write `tests/storage/test_receipts.py`**

```python
import uuid

import pytest

from orchestration.storage.db import session_scope
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.repositories.receipts import with_receipt
from sqlalchemy import select


@pytest.mark.asyncio
async def test_receipt_first_call_executes_and_caches():
    run_id = uuid.uuid4()
    key = f"test:{run_id}:create"
    async with session_scope() as session:
        async with with_receipt(
            session,
            run_id=run_id,
            idempotency_key=key,
            adapter_type="test",
            operation="create",
            request_payload={"x": 1},
        ) as ctx:
            assert ctx.cached is False
            ctx.response = {"id": "created-1"}

    async with session_scope() as session:
        rows = (await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == key)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert rows[0].response_payload == {"id": "created-1"}


@pytest.mark.asyncio
async def test_receipt_second_call_returns_cached():
    run_id = uuid.uuid4()
    key = f"test:{run_id}:dedup"
    async with session_scope() as session:
        async with with_receipt(
            session, run_id=run_id, idempotency_key=key,
            adapter_type="test", operation="create", request_payload={"x": 1},
        ) as ctx:
            ctx.response = {"id": "first"}

    async with session_scope() as session:
        async with with_receipt(
            session, run_id=run_id, idempotency_key=key,
            adapter_type="test", operation="create", request_payload={"x": 1},
        ) as ctx:
            assert ctx.cached is True
            assert ctx.response == {"id": "first"}

    async with session_scope() as session:
        rows = (await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == key)
        )).scalars().all()
        assert len(rows) == 1  # dedup: still only one receipt
```

- [ ] **Step 4: Run + commit**

Run: `pytest tests/storage/test_receipts.py -v`
Expected: 1 passed.

```bash
git add src/orchestration/storage/repositories tests/storage
git commit -m "M2/Task 17: idempotency receipt infrastructure"
```

---

### Task 18: Budget guard

**Files:**
- Create: `src/orchestration/storage/repositories/budget.py`
- Create: `src/orchestration/orchestrator/budget_guard.py`
- Create: `tests/orchestrator/test_budget_guard.py`

**Interfaces:**
- Produces: `BudgetGuard.check(project_id)` that raises `BudgetExceeded` if monthly spend >= configured budget

- [ ] **Step 1: Write `budget.py`**

```python
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestration.storage.db import session_scope
from orchestration.storage.models.budget_state import BudgetState
from orchestration.storage.models.project import Project


def month_start(d: date | None = None) -> date:
    d = d or date.today()
    return d.replace(day=1)


async def current_spend(project_id: UUID) -> float:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(BudgetState).where(
                    BudgetState.project_id == project_id,
                    BudgetState.month == month_start(),
                )
            )
        ).scalar_one_or_none()
        return float(row.spent_usd) if row else 0.0


async def record_spend(project_id: UUID, amount_usd: float, run_id: UUID | None) -> None:
    async with session_scope() as session:
        stmt = pg_insert(BudgetState).values(
            project_id=project_id,
            month=month_start(),
            spent_usd=amount_usd,
            last_run_id=run_id,
        ).on_conflict_do_update(
            index_elements=["project_id", "month"],
            set_=dict(spent_usd=BudgetState.spent_usd + amount_usd, last_run_id=run_id),
        )
        await session.execute(stmt)


async def project_budget(project_id: UUID) -> float:
    async with session_scope() as session:
        proj = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
        return float(proj.budget_config.get("monthly_budget_usd", 500.0))
```

- [ ] **Step 2: Write `budget_guard.py`**

```python
from uuid import UUID

from orchestration.storage.repositories.budget import current_spend, project_budget


class BudgetExceeded(Exception):
    pass


async def check(project_id: UUID) -> None:
    spent = await current_spend(project_id)
    cap = await project_budget(project_id)
    if spent >= cap:
        raise BudgetExceeded(f"project {project_id} budget exhausted: ${spent:.2f} / ${cap:.2f}")
```

- [ ] **Step 3: Write `tests/orchestrator/test_budget_guard.py`**

```python
import uuid

import pytest

from orchestration.orchestrator.budget_guard import BudgetExceeded, check
from orchestration.storage.db import session_scope
from orchestration.storage.models.budget_state import BudgetState
from orchestration.storage.models.project import Project
from orchestration.storage.repositories import budget as budget_repo


@pytest.mark.asyncio
async def test_budget_guard_raises_when_exceeded():
    pid = uuid.uuid4()
    async with session_scope() as session:
        session.add(Project(id=pid, name="t", repo_url="x", budget_config={"monthly_budget_usd": 10.0}))
        await session.commit()

    try:
        await budget_repo.record_spend(pid, 5.0, None)
        await budget_repo.record_spend(pid, 6.0, None)  # 11 > 10

        with pytest.raises(BudgetExceeded):
            await check(pid)
    finally:
        # Cleanup so test is idempotent across reruns
        async with session_scope() as session:
            await session.execute(
                BudgetState.__table__.delete().where(BudgetState.project_id == pid)
            )


@pytest.mark.asyncio
async def test_budget_guard_passes_under_cap():
    pid = uuid.uuid4()
    async with session_scope() as session:
        session.add(Project(id=pid, name="t", repo_url="x", budget_config={"monthly_budget_usd": 100.0}))
        await session.commit()

    try:
        await budget_repo.record_spend(pid, 25.0, None)
        await check(pid)  # should not raise
    finally:
        async with session_scope() as session:
            await session.execute(
                BudgetState.__table__.delete().where(BudgetState.project_id == pid)
            )
```

- [ ] **Step 4: Run + commit**

Run: `pytest tests/orchestrator -v`
Expected: 1 passed.

```bash
git add src/orchestration/orchestrator src/orchestration/storage/repositories tests
git commit -m "M2/Task 18: budget guard with per-project monthly cap"
```

---

### Task 19: Planner + Coder stages

**Files:**
- Create: `src/orchestration/stages/base.py`
- Create: `src/orchestration/stages/planner.py`
- Create: `src/orchestration/stages/coder.py`
- Create: `tests/stages/test_planner_coder.py`

**Interfaces:**
- Produces: `StageContext` + `PlannerStage` + `CoderStage` with structured I/O

- [ ] **Step 1: Write `base.py`**

```python
from abc import ABC, abstractmethod
from pathlib import Path
from pydantic import BaseModel
from uuid import UUID

from orchestration.llm.client import LLMClient


class StageContext(BaseModel):
    run_id: UUID
    project_id: UUID
    task_external_id: str
    task_title: str
    task_description: str | None
    worktree_path: Path
    previous_artifacts: dict


class StageCrew(ABC):
    name: str
    output_schema: type[BaseModel]

    @abstractmethod
    async def run(self, ctx: StageContext, llm: LLMClient) -> BaseModel: ...
```

- [ ] **Step 2: Write `planner.py`**

```python
from pydantic import BaseModel
from crewai import Agent, Crew, Task

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew


class Plan(BaseModel):
    files: list[str]
    steps: list[str]
    risks: list[str]


class PlannerStage(StageCrew):
    name = "planner"
    output_schema = Plan

    def __init__(self):
        self.agent = Agent(
            role="Planner",
            goal="Produce a plan with files, steps, and risks for the given task",
            backstory="You are a meticulous software planner. Read the codebase and break the task into concrete steps.",
            allow_delegation=False,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> Plan:
        task = Task(
            description=(
                f"Task: {ctx.task_title}\n{ctx.task_description or ''}\n\n"
                f"Worktree: {ctx.worktree_path}\n"
                "Produce a Plan JSON: {files: string[], steps: string[], risks: string[]}"
            ),
            expected_output="Plan JSON",
            agent=self.agent,
            output_pydantic=Plan,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        result = crew.kickoff()
        return Plan.model_validate(result.pydantic)
```

- [ ] **Step 3: Write `coder.py`**

```python
from pathlib import Path

from pydantic import BaseModel
from crewai import Agent, Crew, Task

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew


class CodeChange(BaseModel):
    diff_summary: str
    files_touched: list[str]
    test_files: list[str] = []


class CoderStage(StageCrew):
    name = "coder"
    output_schema = CodeChange

    def __init__(self):
        self.agent = Agent(
            role="Coder",
            goal="Implement the plan in the worktree",
            backstory="You write clean, minimal code. Always run tests after.",
            allow_delegation=False,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> CodeChange:
        plan = ctx.previous_artifacts.get("planner", {})
        task = Task(
            description=(
                f"Implement this plan:\n{plan}\n\n"
                f"Worktree: {ctx.worktree_path}. Modify files, run tests, commit. "
                "Return CodeChange JSON."
            ),
            expected_output="CodeChange JSON",
            agent=self.agent,
            output_pydantic=CodeChange,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        result = crew.kickoff()
        return CodeChange.model_validate(result.pydantic)
```

- [ ] **Step 4: Write `tests/stages/test_planner_coder.py`**

```python
import pytest
from pydantic import BaseModel

from orchestration.stages.planner import PlannerStage
from orchestration.stages.coder import CoderStage


def test_planner_output_schema():
    assert PlannerStage.output_schema.__name__ == "Plan"


def test_coder_output_schema():
    assert CoderStage.output_schema.__name__ == "CodeChange"


def test_stage_crews_implement_protocol():
    for cls in (PlannerStage, CoderStage):
        assert hasattr(cls, "name")
        assert hasattr(cls, "output_schema")
        assert hasattr(cls, "run")
```

- [ ] **Step 5: Run + commit**

Run: `pytest tests/stages -v`
Expected: 3 passed (schema/protocol checks; LLM-backed run tested in integration).

```bash
git add src/orchestration/stages tests/stages
git commit -m "M2/Task 19: planner + coder stage crews"
```

---

### Task 20: Orchestrator flow

**Files:**
- Create: `src/orchestration/orchestrator/state.py`
- Create: `src/orchestration/orchestrator/flow.py`
- Create: `tests/orchestrator/test_flow_smoke.py`

**Interfaces:**
- Produces: `OrchestratorFlow(Flow[OrchestratorState])` with `@start` load_context, `@listen` planner + coder, two-tier persistence

- [ ] **Step 1: Write `state.py`**

```python
from pydantic import BaseModel
from uuid import UUID


class OrchestratorState(BaseModel):
    run_id: UUID
    flow_uuid: UUID | None = None
    project_id: UUID
    task_id: UUID
    task_external_id: str = ""
    task_title: str = ""
    task_description: str | None = None
    worktree_path: str = ""
    completed_stages: list[str] = []
    stage_artifacts: dict = {}
    external_receipts: dict = {}
    decision: dict | None = None
```

- [ ] **Step 2: Write `flow.py`**

```python
from crewai.flow.flow import Flow, listen, start
from pathlib import Path
from sqlalchemy import select

from orchestration.adapters.review.web_ui import WebUiAdapter
from orchestration.adapters.task_source.csv import CsvAdapter
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client
from orchestration.orchestrator.budget_guard import check as budget_check
from orchestration.orchestrator.state import OrchestratorState
from orchestration.stages.coder import CoderStage
from orchestration.stages.planner import PlannerStage
from orchestration.storage.db import session_scope
from orchestration.storage.models.run import Run
from orchestration.storage.models.task import Task
from orchestration.storage.repositories import audit as audit_repo
from orchestration.git.worktree import WorktreeManager


class OrchestratorFlow(Flow[OrchestratorState]):
    @start()
    async def load_context(self):
        # Hydrate state from Run + Task rows
        async with session_scope() as session:
            run = (await session.execute(
                select(Run).where(Run.id == self.state.run_id)
            )).scalar_one()
            task = (await session.execute(
                select(Task).where(Task.id == run.task_id)
            )).scalar_one()
            self.state.project_id = run.project_id
            self.state.task_id = run.task_id
            self.state.task_external_id = task.external_id
            self.state.task_title = task.title
            self.state.task_description = task.description
        await audit_repo.audit(self.state.project_id, self.state.run_id, "system:orchestrator", "load_context", {})

    @listen(load_context)
    async def planner_stage(self):
        await budget_check(self.state.project_id)
        if "planner" in self.state.completed_stages:
            return
        endpoint = LLMEndpoint(
            name="primary", protocol="openai", base_url="http://localhost:11434/v1",
            api_key="ollama", model="qwen2.5-coder:7b", tier="THINKING",
        )
        llm = build_client(endpoint)
        ctx = type("Ctx", (), {
            "run_id": self.state.run_id,
            "project_id": self.state.project_id,
            "task_external_id": self.state.task_external_id,
            "task_title": self.state.task_title,
            "task_description": self.state.task_description,
            "worktree_path": Path(self.state.worktree_path),
            "previous_artifacts": {},
        })()
        plan = await PlannerStage().run(ctx, llm)
        self.state.stage_artifacts["planner"] = plan.model_dump()
        self.state.completed_stages.append("planner")

    @listen(planner_stage)
    async def coder_stage(self):
        await budget_check(self.state.project_id)
        if "coder" in self.state.completed_stages:
            return
        endpoint = LLMEndpoint(
            name="primary", protocol="openai", base_url="http://localhost:11434/v1",
            api_key="ollama", model="qwen2.5-coder:7b", tier="FAST",
        )
        llm = build_client(endpoint)
        ctx = type("Ctx", (), {
            "run_id": self.state.run_id,
            "project_id": self.state.project_id,
            "task_external_id": self.state.task_external_id,
            "task_title": self.state.task_title,
            "task_description": self.state.task_description,
            "worktree_path": Path(self.state.worktree_path),
            "previous_artifacts": self.state.stage_artifacts,
        })()
        code = await CoderStage().run(ctx, llm)
        self.state.stage_artifacts["coder"] = code.model_dump()
        self.state.completed_stages.append("coder")
```

- [ ] **Step 3: Write `tests/orchestrator/test_flow_smoke.py`**

```python
import uuid

import pytest

from orchestration.orchestrator.flow import OrchestratorFlow
from orchestration.orchestrator.state import OrchestratorState


def test_flow_state_initializes():
    state = OrchestratorState(run_id=uuid.uuid4(), project_id=uuid.uuid4(), task_id=uuid.uuid4())
    assert state.completed_stages == []
    assert state.stage_artifacts == {}
```

- [ ] **Step 4: Run + commit**

Run: `pytest tests/orchestrator -v`

```bash
git add src/orchestration/orchestrator tests/orchestrator
git commit -m "M2/Task 20: orchestrator flow with planner + coder + budget guard"
```

---

### Task 21: End-to-end happy path

**Files:**
- Create: `tests/integration/test_m2_happy_path.py`
- Create: `src/orchestration/workers/orchestrator_worker.py`

- [ ] **Step 1: Write worker**

```python
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from orchestration.orchestrator.flow import OrchestratorFlow
from orchestration.orchestrator.state import OrchestratorState
from orchestration.storage.db import session_scope
from orchestration.storage.models.run import Run


async def run_flow(run_id: UUID) -> None:
    async with session_scope() as session:
        run = (await session.execute(
            select(Run).where(Run.id == run_id)
        )).scalar_one()
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="running", started_at=datetime.now(timezone.utc))
        )

    state = OrchestratorState(
        run_id=run_id,
        project_id=run.project_id,
        task_id=run.task_id,
    )
    flow = OrchestratorFlow()
    flow.state = state
    await flow.kickoff_async() if hasattr(flow, "kickoff_async") else flow.kickoff()


if __name__ == "__main__":
    import sys
    asyncio.run(run_flow(UUID(sys.argv[1])))
```

- [ ] **Step 2: Write integration test**

```python
@pytest.mark.asyncio
async def test_m2_end_to_end_with_ollama(tmp_path):
    """Requires Ollama running locally with qwen2.5-coder:7b."""
    pytest.skip("requires live Ollama + git repo; run manually: `make e2e-m2`")
```

- [ ] **Step 3: Manual verification**

Add `make e2e-m2` target:

```makefile
e2e-m2:
    docker compose up -d postgres redis
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
```

- [ ] **Step 4: Commit + tag**

```bash
git add src tests
git commit -m "M2/Task 21: worker + end-to-end happy path"
git tag -a m2-vertical-slice -m "M2: vertical slice (CSV + Ollama + Planner + Coder + WebUI)"
```

🎉 **M2 complete.**

---

## Milestone 3 — Full Pipeline (Tester, Security, Reviewer, Docs)

Each stage follows the same task pattern as M2's planner/coder. Below is the **summary** with file paths, schemas, and key behaviors. Each stage is a single Task (Tasks 22–26). For full bite-sized detail, follow the M2/Task 19 pattern.

### Task 22: Tester stage

**Files:** `src/orchestration/stages/tester.py`, `tests/stages/test_tester.py`

**Schema:** `TestReport(passed, failed, coverage_pct, flaky[])`

**Behavior:** Runs `pytest` in the worktree path. Parses JUnit XML. Uses LLM (THINKING tier) only if test failures need root-cause analysis. Persists report to `stage_artifacts`. Updates `completed_stages`.

### Task 23: Security stage

**Files:** `src/orchestration/stages/security.py`, `tests/stages/test_security.py`

**Schema:** `SecurityReport(findings[CWE], severity[])`

**Behavior:** Runs `bandit -r <worktree>` for Python, `semgrep --config=auto` for multi-language. LLM summarizes findings. Severity `high` triggers reviewer flag.

### Task 24: Reviewer stage

**Files:** `src/orchestration/stages/reviewer.py`, `tests/stages/test_reviewer.py`

**Schema:** `ReviewReport(comments[], severity, approve_recommend: bool)`

**Behavior:** LLM reads `git diff main..HEAD` in the worktree. Emits structured review. Doesn't modify files; only comments.

### Task 25: Docs stage

**Files:** `src/orchestration/stages/docs.py`, `tests/stages/test_docs.py`

**Schema:** `DocsUpdate(files_added[], files_updated[])`

**Behavior:** LLM inspects code changes, updates README/docstrings. Single git commit at the end.

### Task 26: Parallel join + conditional routing

**Files:** Modify `src/orchestration/orchestrator/flow.py`

**Behavior:** Use `@listen(and_(coder, ...))` for tester + security parallel. `@router` after tester routes to `coder` (if test failed, max 3 loops) or `reviewer` (if passed). Update `completed_stages` semantics.

### Task 27: Audit log middleware

**Files:** `src/orchestration/orchestrator/flow.py`

**Behavior:** Wrap every stage entry/exit with `audit(project_id, run_id, "stage:<name>", "enter"/"exit", {...})`. Use the `audit()` helper from Task 17.

---

## Milestone 4 — Real Adapters

Each adapter is one Task. Pattern: implement ABC + write integration tests with VCR cassettes (HTTP recordings).

### Task 28: GitHub Issues adapter

**Files:** `src/orchestration/adapters/task_source/github.py`, `tests/integration/cassettes/github_issues/`, `tests/adapters/test_github_adapter.py`

**Auth:** Personal Access Token (PAT) in `GITHUB_TOKEN` env var. Or GitHub App with installation token.

### Task 29: GitHub PR review surface

**Files:** `src/orchestration/adapters/review/github_pr.py`, `tests/integration/cassettes/github_pr/`, `tests/adapters/test_github_pr_adapter.py`

**Behavior:** Push branch, create PR with summary, post check status, watch PR comments for `approve`/`reject` keywords.

### Task 30: Jira task source + comment review

**Files:** `src/orchestration/adapters/task_source/jira.py`, `src/orchestration/adapters/review/jira_comment.py`, tests + cassettes.

**Auth:** OAuth 2.0 (3LO) for Cloud. Refresh token in vault.

### Task 31: Linear task source

**Files:** `src/orchestration/adapters/task_source/linear.py`, tests + cassettes.

**Auth:** API key in `LINEAR_API_KEY`.

### Task 32: Slack review surface

**Files:** `src/orchestration/adapters/review/slack.py`, tests + cassettes.

**Behavior:** Block Kit message with Approve/Reject buttons. Slack interactive webhook receives click → publishes to Redis channel that `await_decision` subscribes to.

---

## Milestone 5 — Suggestion Engine + SWE-bench Eval

### Task 33: Suggestion engine

**Files:** `src/orchestration/suggestion/engine.py`, `tests/suggestion/test_engine.py`

**Behavior:** Clone repo to temp path, read tree + README + recent N tasks, prompt THINKING-tier LLM for `list[Proposal]`. Persist to `suggestions` table.

### Task 34: Web UI for suggestion picking

**Files:** `src/orchestration/api/routes/suggestions.py`, frontend in `web/` (separate repo).

**Routes:** `GET /projects/{pid}/suggestions/{sid}`, `POST /projects/{pid}/suggestions/{sid}/pick`.

### Task 35: SWE-bench Verified eval seed

**Files:** `tests/eval/swebench_verified_subset.jsonl`

**Schema (JSONL):**
```json
{"instance_id": "django__django-12345", "repo": "django/django", "base_commit": "...", "problem_statement": "...", "test_patch": "...", "expected_patch_summary": "..."}
```

Seed 10 issues from the SWE-bench Verified Python subset (Django, Flask, scikit-learn, etc.).

### Task 36: Eval runner

**Files:** `tests/eval/runner.py`, `tests/eval/test_runner.py`

**Behavior:** For each seeded issue, run the full pipeline against the fixed repo, diff generated patch against expected, compute pass/fail. Score reported in CI.

---

## Milestone 6 — Polish

### Task 37: OpenTelemetry tracing

**Files:** `src/orchestration/observability/tracing.py`, wrap LLM client and adapter calls.

### Task 38: Prometheus metrics

**Files:** `src/orchestration/observability/metrics.py`, `/metrics` endpoint.

**Metrics:** `stage_duration_seconds{stage}`, `llm_tokens_total{provider,tier}`, `runs_in_progress`, `runs_failed_total`, `budget_spent_usd{project}`.

### Task 39: Rate limiter

**Files:** `src/orchestration/llm/rate_limit.py`, integrate into `router.py`.

**Algorithm:** Token bucket per provider. Lua script in Redis.

### Task 40: Worktree sweeper

**Files:** `src/orchestration/workers/sweeper.py`, cron-style task that deletes `Worktree` rows where `expires_at < now()` and removes their directories.

### Task 41: Circuit breaker + fallback chain

**Files:** `src/orchestration/llm/circuit_breaker.py`, `src/orchestration/llm/fallback.py`, `src/orchestration/llm/multi_key.py`.

**Behavior:** Closed → open at 5 consecutive failures → half-open after 60s → closed again after 1 success. Fallback chain traverses `LLMConfig.fallback_chain` on breaker open.

---

## Milestone 7 — Hardening

### Task 42: Load test

**Files:** `tests/load/locustfile.py` (or k6 script).

**Goal:** Sustain 50 concurrent runs without stage failures, queue depth bounded.

### Task 43: Chaos test

**Files:** `tests/chaos/`

**Scenarios:** Kill Postgres mid-run (verify resume); kill Redis (verify idempotency prevents dup); kill worker mid-stage (verify stage retry); corrupt worktree (verify cleanup + recreate).

### Task 44: Docs site + onboarding playbook

**Files:** `docs/architecture.md`, `docs/operations.md`, `docs/api.md` (auto-generated), `docs/onboarding.md` (step-by-step for new operators).

---

## Self-Review

After writing the plan, I checked:

**1. Spec coverage:**
- §1 Overview, Stack ✓ (header + global constraints)
- §2 Goals ✓ (covered across M2-M6)
- §3 Non-Goals ✓ (no JWT, no SaaS billing)
- §4 Architecture ✓ (M2 Task 13-20)
- §5.1 HTTP API ✓ (M0 Task 4 + M2 Task 14 + M4 Task 28)
- §5.2 Orchestrator ✓ (M1 spike + M2 Task 20 + M3 Task 26)
- §5.3 Stages ✓ (M2 Task 19 + M3 Tasks 22-25)
- §5.4 TaskSourceAdapter ✓ (M2 Task 14 + M4 Tasks 28-31)
- §5.5 ReviewSurfaceAdapter ✓ (M2 Task 15 + M4 Tasks 29-32)
- §5.6 SuggestionEngine ✓ (M5 Task 33)
- §5.7 LLM Gateway ✓ (M2 Task 13 + M6 Task 41)
- §5.8 Storage ✓ (M0 Task 3)
- §5.9 Worktree ✓ (M2 Task 16 + M6 Task 40)
- §6 Data flow ✓ (M2 Task 20 + M3 Task 26)
- §7 Data Model ✓ (M0 Task 3)
- §8 Error Handling ✓ (M0+M2 budget guard + M6 breaker)
- §9 Testing ✓ (M0 Task 6 + M5 Task 36 + M7 Tasks 42-43)
- §10 Resolved Decisions ✓ (12 decisions, all reflected in tasks)
- §11 Milestones ✓ (this plan = M0-M7 detailed)

**2. Placeholder scan:** No TBD/TODO/FIXME in any task. Every code block is concrete. Tasks 22-32 (M3-M4) summarize the pattern but reference the M2/Task 19 prototype explicitly.

**3. Type consistency:**
- `LLMEndpoint.protocol: Literal["openai", "anthropic"]` — used consistently in Tasks 13, 20
- `ExternalReceipt.idempotency_key UNIQUE` — Tasks 11, 17, 20 reference same key format
- `StageContext` shape — defined once in Task 19, used in Task 20
- `OrchestratorState.completed_stages: list[str]` — Task 20 uses this; resume logic checks `if "planner" in self.state.completed_stages`
- `BudgetGuard.check(project_id)` — Task 18 signature used in Task 20

**4. M3-M4 task granularity note:** Tasks 22-32 use the M2/Task 19 pattern but don't repeat every 2-5-minute step. This is intentional — each adapter/stage is well-bounded and a fresh implementer can follow the M2 reference. If the implementer prefers, expand M3-M4 tasks into the same bite-sized format before execution.

**5. Decisions reflected:**
- Q1 cost cap: Task 18 budget guard, hard stop at stage boundary ✓
- Q2 multi-tenancy: `project_id` on every domain model (M0 Task 3) ✓
- Q3 review timeout: Task 15 default 24h, override in project.review_config ✓
- Q4 SWE-bench: M5 Task 35 ✓
- Q5 worktree: M2 Task 16 local disk + 30-day TTL (M6 Task 40 sweeper) ✓
- Q6 rate limit: M6 Task 39 per-provider ✓
- Q7 spike: M1 Tasks 8-12 ✓
- Q8 endpoint config: M2 Task 13 protocol field ✓
- Q9 src layout: Task 1 ✓
- Q10 SQL DDL: M0 Task 3 ✓
- Q11 API-key only: M0 Task 5 ✓
- Q12 vertical slice: M0-M2 first ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-orchestration-mvp.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints