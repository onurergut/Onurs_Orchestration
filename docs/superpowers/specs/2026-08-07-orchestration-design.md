# Orchestration System — Resolved Design Spec

**Date:** 2026-08-07
**Status:** Approved, ready for implementation planning
**Supersedes:** `2026-08-06-orchestration-design.md` (gaps filled; structure preserved)
**Author:** Brainstorming session with user (12 questions resolved)

## 1. Overview

A self-hosted backend service that orchestrates multi-agent software engineering workflows. The system integrates with task management systems (Jira/Linear/GitHub Issues), suggests new work from codebase analysis, executes tasks through a 7-stage agent pipeline, and submits results to a pluggable human review surface.

**Target users:** Engineering teams with code IP concerns, multi-LLM strategies, Jira/Linear workflows, and audit/compliance requirements.

**Stack:** Python 3.12, FastAPI, CrewAI Flow, Pydantic v2, SQLAlchemy 2.0 async, PostgreSQL 16, Redis 7, Docker Compose. LLM-agnostic via pluggable endpoints (OpenAI-compatible or Anthropic-compatible).

## 2. Goals

- Integrate with task management systems without vendor lock-in (pluggable adapter)
- Execute tasks via explicit, resumable 7-stage pipeline (Planner → Coder → Tester → Reviewer → Security → Docs → Human)
- Suggest new tasks from codebase + historical patterns (on-demand)
- Submit completed work to a pluggable review surface (GitHub PR, Slack, Jira, Web)
- Persist audit trail (every action, every external side-effect)
- Allow any LLM provider compatible with OpenAI or Anthropic API shape (endpoint + key configurable)
- Self-host with Docker Compose, single-org per deployment
- Per-project row-level isolation via `tenant_id`
- Per-project monthly LLM budget with hard-stop at stage boundary

## 3. Non-Goals (v1)

- Trajectory learning from past runs (v2)
- Agentic MapReduce for whole-codebase scans (v2)
- Fleet of parallel sub-agents (v3)
- Full Devin-Review-style PR review tool (basic reviewer stage only in v1)
- Multi-tenant SaaS billing (single-org per deployment)
- JWT / OAuth login (API-key only in v1)
- IDE plugin (CLI + Web UI only in v1)

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  External Adapters (pluggable)                              │
│  ┌──────────┐ ┌──────────┐ ┌────────┐ ┌──────────────┐      │
│  │ Jira     │ │ Linear   │ │ GH Iss │ │ CSV upload   │      │
│  └────┬─────┘ └────┬─────┘ └───┬────┘ └──────┬───────┘      │
│       │ TaskSourceAdapter interface                          │
│       ▼                                                     │
│  ┌──────────────────────────────────────────────────┐       │
│  │  HTTP API (FastAPI, API-key auth)                │       │
│  │  POST /tasks/suggest                             │       │
│  │  POST /projects/{pid}/tasks/{tid}/execute        │       │
│  │  GET  /runs/{rid}                                │       │
│  │  POST /runs/{rid}/approve|reject|comment         │       │
│  │  POST /webhooks/{adapter}                        │       │
│  └────────────────────┬─────────────────────────────┘       │
│                       │                                     │
│                       ▼                                     │
│  ┌──────────────────────────────────────────────────┐       │
│  │  Orchestrator (CrewAI Flow)                      │       │
│  │  - @persist: FlowState (plan, artifacts, audit)  │       │
│  │  - checkpoint=True: execution pointer            │       │
│  │  - @router: conditional stage routing            │       │
│  │  - Stage-level idempotency via receipts          │       │
│  │  - Budget guard at stage boundary                │       │
│  └────────────────────┬─────────────────────────────┘       │
│                       │                                     │
│       ┌───────────────┼───────────────┐                     │
│       ▼               ▼               ▼                     │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐                  │
│  │ Stage:  │    │ Stage:  │    │ Stage:  │  ...             │
│  │Planner  │───▶│Coder    │───▶│Tester   │                  │
│  └─────────┘    └─────────┘    └─────────┘                  │
│   (1-3 agents)  (1-3 agents)  (parallel w/Security)         │
│                       │                                     │
│                       ▼                                     │
│  ┌──────────────────────────────────────────────────┐       │
│  │  LLM Gateway                                     │       │
│  │  OpenAI/Anthropic compatible endpoint adapter     │       │
│  │  Tier (FAST|THINKING) + circuit breaker +        │       │
│  │  fallback chain + per-provider rate limit         │       │
│  └──────────────────────────────────────────────────┘       │
│                       │                                     │
│                       ▼                                     │
│  ┌──────────────────────────────────────────────────┐       │
│  │  Storage (Postgres) + Cache/Queue (Redis)         │       │
│  └──────────────────────────────────────────────────┘       │
│                       │                                     │
│       ┌───────────────┼───────────────┐                     │
│       ▼               ▼               ▼                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐                  │
│  │ GitHub   │ │ Slack    │ │ Jira comment │                  │
│  │ PR       │ │ /approve │ │ + watch      │                  │
│  └──────────┘ └──────────┘ └──────────────┘                  │
│  ReviewSurfaceAdapter interface                             │
└─────────────────────────────────────────────────────────────┘
```

### Orchestrator refinements

- **Two-tier persistence:** `@persist` saves FlowState (artifacts, plan content); `checkpoint=True` saves execution pointer (`completed_stages`). Flow always re-enters at `@start`, but stages check `completed_stages` and skip if done. Resume via `restore_from_state_id`.
- **Stage atomicity:** Pause boundaries only between stages. A stage runs to completion or fails atomically — never half-done. Within a stage, retries re-execute the whole stage; idempotency on side-effects prevents duplicates from those retries.
- **Idempotency on side-effects:** Every external call uses a deterministic `idempotency_key`. On resume or stage retry, the adapter checks for an existing receipt with that key; if found, it returns the existing record and skips re-execution. Prevents duplicate Jira tickets, PRs, Slack messages on retry/resume.
- **Conditional routing:** `@router` after each stage. Test fail → coder (max 3 loops). Security findings high → reviewer flagged. Linear pipeline is a default, not the only path.
- **Budget guard:** Before entering any stage, the orchestrator checks the project's current monthly spend against its configured budget. If exhausted (or projected to exceed within the stage), the stage is skipped and the run is paused for human approval to raise the budget.
- **Pre-implementation spike:** A small CrewAI Flow POC (kill mid-run, resume, parallel stages, idempotency on external calls) validates the persistence model before the full orchestrator is built. Spec is amended based on findings.

### LLM Gateway refinements

- **Pluggable endpoints:** Every provider is configured as `(endpoint_url, api_key, model_name, protocol: "openai"|"anthropic")`. Any OpenAI-compatible service (Ollama, vLLM, Together, Groq, LM Studio) or Anthropic-compatible service (Anthropic API, Bedrock with proxy) is usable.
- **Tier abstraction:** `FAST | THINKING`. Planner THINKING, Coder FAST by default. Per-project override via `LLMConfig`.
- **Per-provider rate limit (global):** Token-bucket in Redis, key `ratelimit:{provider}`. One project pattering cannot starve others within the same provider pool.

## 5. Components

### 5.1 HTTP API (`src/orchestration/api/`)

Thin entrypoint. Validate, queue, respond. No state. API-key auth via `X-API-Key` header.

```
POST   /projects/{pid}/tasks/suggest
POST   /projects/{pid}/tasks/{tid}/execute
GET    /runs/{rid}
POST   /runs/{rid}/approve | reject | comment
GET    /projects/{pid}/runs
POST   /webhooks/{adapter}
GET    /health, /ready
```

OpenAPI auto-generated.

### 5.2 Orchestrator (`src/orchestration/orchestrator/`)

CrewAI Flow state machine. Determines next stage, handles resume, routes conditionally, enforces budget guard.

The code below is the concrete `crewai` 1.15.12 API surface (verified in `docs/spike-m1.md`); it is the pattern copied into M2/Task 20.

```python
from uuid import UUID, uuid4

from crewai.flow import (
    Flow,
    FlowPersistence,
    CheckpointConfig,
    and_,
    listen,
    persist,
    start,
)
from pydantic import BaseModel, Field


class OrchestratorState(BaseModel):
    # Library-required: @persist() refuses to stamp a state without an `id`
    # field. The runtime uses this as the primary key in the persistence
    # backend (SQLiteFlowPersistence.flow_states by default). It is the
    # flow's persistence key, NOT the user-facing run identifier — for
    # that, see `run_id` below.
    id: UUID = Field(default_factory=uuid4)

    run_id: UUID            # application-level orchestration run id
    project_id: UUID
    task_id: str
    completed_stages: set[str] = Field(default_factory=set)
    stage_artifacts: dict[str, Artifact] = Field(default_factory=dict)
    external_receipts: dict[str, Receipt] = Field(default_factory=dict)
    decision: Decision | None = None


@persist()
class OrchestratorFlow(Flow[OrchestratorState]):
    @start()
    def load_context(self): ...

    @listen(load_context)
    def planner_stage(self): ...

    @router(planner_stage)
    def route_after_planner(self) -> str: ...

    @listen(route_after_planner)
    def coder_stage(self): ...

    # tester_stage + security_stage run in parallel after coder; the
    # runtime fans branches out via the default ThreadPoolExecutor and
    # the @listen(and_(...)) join only fires once both have completed.
    @listen(and_(tester_stage, security_stage))  # join
    def reviewer_stage(self): ...

    # ... docs, human_review
```

**Persistence + checkpoint wiring (per `docs/spike-m1.md`):**

```python
flow = OrchestratorFlow(
    persistence=FlowPersistence(),                   # SQLiteFlowPersistence by default
    checkpoint=CheckpointConfig(
        location="./.checkpoints",
        on_events=["*"],                             # or a tighter event list
    ),
)
result = flow.kickoff()                              # sync; see notes below
```

The two equivalent ways to wire persistence are the class-level `@persist()`
decorator (shown above) and the constructor `persistence=` argument. The
spike showed both work; either is acceptable. The library's
`default_flow_persistence()` is a `SQLiteFlowPersistence` at a user-data
path, fine for the spike but not for production — the M2/M3 work to swap in a
Postgres-backed `FlowPersistence` is out of scope for the spec.

#### CrewAI Flow adaptation notes

- **`kickoff()` is sync.** `kickoff_async()` is the async variant. Async
  callers (FastAPI, asyncio) must use `kickoff_async()` directly or wrap the
  sync call in `asyncio.to_thread(flow.kickoff)`. The parallel-stage
  implementation underneath is `asyncio.gather` over `_execute_method`
  tasks, so async listeners receive true async semantics; sync listeners
  run on the default ThreadPoolExecutor.

- **`restore_from_state_id` forks, not resumes.** The runtime hydrates
  `self.state` from the persisted snapshot, mints a fresh `id`, then runs
  every method on the flow from `@start` again. Each stage is therefore
  executed at least twice on restore — the first time it ran originally,
  and again on the restored flow. Stages MUST self-guard via
  `completed_stages`:
  ```python
  @listen(load_context)
  def planner_stage(self):
      if "planner_stage" in self.state.completed_stages:
          return
      # ... do work, then:
      self.state.completed_stages.add("planner_stage")
  ```
  The contract is "every stage is idempotent on `completed_stages`
  membership." If true resume semantics (skip already-completed methods)
  are needed, use `Flow.from_checkpoint` / `Flow.fork` against the
  `CheckpointConfig` JSON artifacts instead of `restore_from_state_id`.

- **`@persist()` is metadata only and MUST be called with parens.** Bare
  `@persist` silently replaces the wrapped method with `persist`'s inner
  closure, producing empty state at runtime. Actual persistence is driven
  by the Flow's `persistence=` constructor argument
  (`SQLiteFlowPersistence` by default) or by `default_flow_persistence()`
  when neither decorator nor constructor injection is supplied.

- **`checkpoint=True` writes nothing for a Flow.** The default-on-True
  config (`CheckpointConfig(on_events=["task_completed"])`) targets a Crew
  event Flow never fires. Pass an explicit
  `CheckpointConfig(location=..., on_events=["*"])` or a tighter list
  (e.g. `["flow_started", "method_execution_finished", "flow_finished"]`)
  to get useful artifacts.

- **`flow_uuid` collapsed into `id`.** The original spec carried a
  `flow_uuid: UUID` field. The library's persistence key is `state.id` —
  there is no `flow_uuid` in the library API. The `flow_uuid` field was
  removed; `state.id` is the persistence key and `state.run_id` is the
  application-level orchestration run identifier. The two are distinct
  concerns: `id` is generated per Flow instance by `uuid4()` and is what
  the persistence backend indexes by; `run_id` is assigned upstream by
  the API/DB layer and is what users see in `GET /runs/{rid}`.

- **`pyproject.toml` floor tightening.** `pyproject.toml` pins
  `crewai>=0.86`, which resolves to 1.15.12 today but leaves the spec
  exposed to whatever minor version the lockfile picks up. Recommend
  tightening to `crewai>=1.15,<2` so CI reproducibly resolves to the API
  surface documented here.

### 5.3 Stage Crews (`src/orchestration/stages/`)

7 small CrewAI crews, each 1–3 agents.

| Stage | Agents | Output schema |
|---|---|---|
| planner | Planner, Research | `Plan(files[], steps[], risks[])` |
| coder | Coder | `CodeChange(diff_summary, files_touched[], test_files[])` |
| tester | TestRunner, Fixer | `TestReport(passed, failed, coverage_pct, flaky[])` |
| reviewer | Reviewer | `ReviewReport(comments[], severity, approve_recommend: bool)` |
| security | Security | `SecurityReport(findings[CWE], severity[])` |
| docs | Docs | `DocsUpdate(files_added[], files_updated[])` |
| human_review | — | `Decision(approve, reject, comment)` |

Common shape:
```python
class StageCrew(Protocol):
    name: str
    output_schema: type[BaseModel]
    async def run(self, ctx: StageContext, llm: LLMClient) -> BaseModel: ...
```

### 5.4 Task Source Adapter (`src/orchestration/adapters/task_source/`)

External task management systems.

```python
class TaskSourceAdapter(ABC):
    async def list_tasks(self, project_id: UUID, status: str | None) -> list[ExternalTask]: ...
    async def get_task(self, task_id: str) -> ExternalTask: ...
    async def create_task(self, payload: CreateTask) -> ExternalTask: ...
    async def update_status(self, task_id: str, status: str) -> None: ...
    async def add_comment(self, task_id: str, body: str) -> Comment: ...
```

Implementations: `JiraAdapter` (Cloud REST v3 + OAuth 2.0), `LinearAdapter` (GraphQL), `GitHubIssuesAdapter` (REST v3 + PAT), `CsvAdapter` (dev/test).

### 5.5 Review Surface Adapter (`src/orchestration/adapters/review/`)

Human-in-the-loop channel.

```python
class ReviewSurfaceAdapter(ABC):
    async def notify_review(self, run: Run, artifacts: Artifacts) -> Receipt: ...
    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision: ...
    async def post_comment(self, receipt: Receipt, body: str) -> None: ...
```

Implementations: `GitHubPrAdapter` (branch push + PR + check status), `JiraCommentAdapter` (transition + comment + watcher), `SlackAdapter` (Block Kit with buttons), `WebUiAdapter` (internal review page).

### 5.6 Suggestion Engine (`src/orchestration/suggestion/`)

On-demand codebase + historical task analysis.

```python
class SuggestionEngine:
    async def suggest(self, project_id: UUID) -> list[Proposal]: ...

class Proposal(BaseModel):
    title: str
    description: str
    rationale: str
    estimated_complexity: Literal['small', 'medium', 'large']
    related_files: list[str]
```

Pipeline:
1. Clone repo (worktree) → read tree + key files + README
2. Read recent N tasks via TaskSourceAdapter
3. LLM analysis (THINKING tier) → list[Proposal]
4. Persist as Suggestion row (status=pending)
5. Human picks via Web UI → TaskSourceAdapter.create_task() per picked proposal
6. Picked proposals enter the execute flow (Path 1)

### 5.7 LLM Provider Gateway (`src/orchestration/llm/`)

```python
class LLMClient(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def complete_structured(self, request: CompletionRequest, schema: type[T]) -> T: ...
```

Endpoint configuration:
```python
class LLMEndpoint(BaseModel):
    name: str                          # logical name, e.g. "primary", "fallback-1"
    protocol: Literal["openai", "anthropic"]
    base_url: str                      # e.g. https://api.anthropic.com or http://localhost:11434/v1
    api_key: SecretStr
    model: str                         # model identifier for this endpoint
    tier: Literal["FAST", "THINKING"]  # what tier this endpoint serves
```

Internal:
- `ProviderRegistry` — name → provider (from `LLMEndpoint` configs)
- `ProviderRouter` — strategies: capability_first, cost_first, latency_first
- `CircuitBreaker` — closed/open/half-open (threshold 5, timeout 60s)
- `FallbackChain` — primary fail → next provider
- `MultiKeyPool` — round-robin per endpoint (billing separation)
- `RateLimiter` — token-bucket per provider, global (Q6 decision)
- Unified response shape (provider-agnostic)

Per-project `LLMConfig` overrides tier→endpoint mapping.

### 5.8 Storage (`src/orchestration/storage/`)

Postgres (state) + Redis (queue/cache/sessions).

See §7 for full DDL.

Redis keys: `idempotency:{key}`, `ratelimit:{tenant}:{provider}`, `circuit:{provider}`, `webhook_pending:{receipt_id}`, `session:{run_id}`, `budget:{project_id}:{month}`.

### 5.9 Git Worktree Manager (`src/orchestration/git/`)

```python
class WorktreeManager:
    async def create(self, project_id: UUID, task_id: str) -> Worktree: ...
    async def diff(self, worktree_id: UUID) -> Diff: ...
    async def commit(self, worktree_id: UUID, message: str) -> Commit: ...
    async def push_branch(self, worktree_id: UUID) -> BranchRef: ...
    async def cleanup(self, worktree_id: UUID) -> None: ...
```

Per-task isolated worktree: `git worktree add -b orch/{task_id} /var/worktrees/{id} main`. Each stage runs in that path. Diff snapshot saved as artifact. Branch pushed at final stage (PR adapter handles PR creation).

**Storage:** Local disk at `/var/worktrees/`. Single-host deployment assumption.
**TTL:** 30 days post-run-completion (configurable per project). Background sweeper task deletes expired worktrees.

## 6. Data Flow

### Path 1: Execute task

> **budget_guard note:** Every stage below is preceded by a budget check (omitted in the step list for brevity; see §5.2 and §8). The check runs before stage entry, not mid-stage, to preserve atomicity.

1. POST execute → API validates (API-key), writes Run row, enqueues orchestrator.run
2. Worker kicks off OrchestratorFlow
3. load_context → fetch task, create worktree
4. planner_stage → Plan artifact
5. router → coder (default)
6. coder_stage → CodeChange + git commit + receipt
7. tester_stage + security_stage in parallel
8. router → if test failed (max 3 loops) → coder, else reviewer
9. reviewer_stage → ReviewReport
10. docs_stage → DocsUpdate + git commit
11. human_review_stage → ReviewSurfaceAdapter.notify_review + await_decision
12. approve → push branch + create PR + update task status=done
13. reject → update task status=rejected, notify
14. timeout (per-project default 24h, override via `review_config.review_timeout_seconds`) → default reject

### Path 2: Suggestion

1. POST suggest → Suggestion row (pending)
2. SuggestionEngine.suggest → repo + tasks + LLM → proposals
3. Human picks → TaskSourceAdapter.create_task per picked → receipts
4. (Optional) Auto-execute → Path 1

### Path 3: Resume

1. POST resume (or auto-restart from queue)
2. OrchestratorFlow.kickoff(restore_from_state_id=run.flow_uuid)
3. Each stage checks completed_stages → skip if done
4. Side-effects via idempotency_key → no duplicate
5. **budget_guard** re-evaluates at each stage entry

## 7. Data Model (Full SQL DDL)

### Idempotency key conventions

```
jira_create_task:    f"task:{project_id}:{task_id}:create"
jira_comment:        f"comment:{project_id}:{task_id}:{stage}:{attempt}"
github_pr_create:    f"pr:{project_id}:{task_id}:create"
github_pr_comment:   f"pr_comment:{project_id}:{task_id}:{stage}"
slack_msg:           f"slack:{project_id}:{task_id}:notify"
git_branch_push:     f"branch_push:{project_id}:{task_id}"
```

### Postgres schema

```sql
-- Tenancy: every domain row carries project_id (acts as tenant_id).
-- SQLAlchemy session-level filter enforces isolation; views can add RLS later.

CREATE TABLE projects (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    default_branch  TEXT NOT NULL DEFAULT 'main',
    llm_config_id   UUID,  -- FK added below
    review_config   JSONB NOT NULL DEFAULT '{}'::jsonb,  -- includes review_timeout_seconds override
    budget_config   JSONB NOT NULL DEFAULT '{}'::jsonb,  -- monthly_budget_usd, etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id              UUID PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id),
    key_hash        TEXT UNIQUE NOT NULL,  -- bcrypt/argon2 hash of X-API-Key
    name            TEXT NOT NULL,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_api_keys_user ON api_keys(user_id);

CREATE TABLE llm_configs (
    id              UUID PRIMARY KEY,
    project_id      UUID REFERENCES projects(id),  -- NULL means global default
    endpoints       JSONB NOT NULL,  -- list[LLMEndpoint]
    tier_map        JSONB NOT NULL,  -- {"FAST": "primary", "THINKING": "primary-thinking"}
    fallback_chain  JSONB NOT NULL DEFAULT '[]'::jsonb,
    rate_limit      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-endpoint RPS/tokens
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE projects
    ADD CONSTRAINT projects_llm_config_fk FOREIGN KEY (llm_config_id) REFERENCES llm_configs(id);

CREATE TABLE tasks (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    external_id     TEXT NOT NULL,  -- Jira KEY, Linear id, GH issue number
    source_type     TEXT NOT NULL CHECK (source_type IN ('jira','linear','github','csv')),
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, external_id)
);
CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);

CREATE TABLE runs (
    id                  UUID PRIMARY KEY,
    project_id          UUID NOT NULL REFERENCES projects(id),
    task_id             UUID NOT NULL REFERENCES tasks(id),
    flow_uuid           UUID NOT NULL UNIQUE,  -- CrewAI Flow state id
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','awaiting_review','paused',
                                          'succeeded','failed','rejected','cancelled')),
    current_stage       TEXT,
    completed_stages    TEXT[] NOT NULL DEFAULT '{}',
    idempotency_key     TEXT UNIQUE,  -- for POST execute dedup
    budget_snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_runs_project_status ON runs(project_id, status);
CREATE INDEX idx_runs_task ON runs(task_id);

CREATE TABLE stage_artifacts (
    id              UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    attempt         INT  NOT NULL DEFAULT 1,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, stage, attempt)
);
CREATE INDEX idx_stage_artifacts_run ON stage_artifacts(run_id);

CREATE TABLE external_receipts (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idempotency_key     TEXT UNIQUE NOT NULL,
    adapter_type        TEXT NOT NULL,
    operation           TEXT NOT NULL,  -- e.g. "create_task", "post_comment"
    external_id         TEXT,           -- Jira KEY, PR number, Slack ts
    request_payload     JSONB NOT NULL,
    response_payload    JSONB,
    status              TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);
CREATE INDEX idx_receipts_run ON external_receipts(run_id);
CREATE INDEX idx_receipts_external ON external_receipts(adapter_type, external_id);

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES runs(id) ON DELETE SET NULL,
    project_id      UUID NOT NULL,
    actor           TEXT NOT NULL,  -- user email, "system", "stage:planner", etc.
    action          TEXT NOT NULL,  -- free-form verb
    payload         JSONB NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_run_ts ON audit_log(run_id, ts);
CREATE INDEX idx_audit_project_ts ON audit_log(project_id, ts);

CREATE TABLE suggestions (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','picked','rejected','executed')),
    proposals       JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX idx_suggestions_project_status ON suggestions(project_id, status);

CREATE TABLE llm_usage (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES runs(id) ON DELETE SET NULL,
    project_id      UUID NOT NULL,
    endpoint_name   TEXT NOT NULL,
    model           TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('FAST','THINKING')),
    prompt_tokens   INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10,6) NOT NULL DEFAULT 0,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_usage_project_month ON llm_usage(project_id, date_trunc('month', ts));
CREATE INDEX idx_llm_usage_run ON llm_usage(run_id);

CREATE TABLE worktrees (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    run_id          UUID REFERENCES runs(id) ON DELETE SET NULL,
    path            TEXT NOT NULL,
    branch          TEXT NOT NULL,
    base_sha        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived','deleted')),
    expires_at      TIMESTAMPTZ,  -- TTL = 30 days post-run-finished by default
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleaned_at      TIMESTAMPTZ
);
CREATE INDEX idx_worktrees_project_status ON worktrees(project_id, status);
CREATE INDEX idx_worktrees_expires ON worktrees(expires_at) WHERE status = 'archived';

CREATE TABLE budget_state (
    project_id      UUID NOT NULL REFERENCES projects(id),
    month           DATE NOT NULL,  -- first day of month
    spent_usd       NUMERIC(10,6) NOT NULL DEFAULT 0,
    last_run_id     UUID REFERENCES runs(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, month)
);
```

## 8. Error Handling

| Failure | Handling |
|---|---|
| LLM transient (rate limit, 5xx, timeout) | Circuit breaker → fallback. Retry 3x exponential |
| LLM quality (schema parse fail, low confidence) | Retry 2x with feedback. Then escalate to human |
| Tool/sandbox (git push fail, test fail) | Stage retry 3x. Then mark stage failed or escalate |
| External API (Jira 503, Slack down) | Adapter retry. Receipt idempotency → no dup |
| Side-effect dup on resume | Adapter checks idempotency_key, returns existing receipt |
| Stage fatal (Planner can't make plan) | Run.status=failed. Task stays "blocked" in Jira |
| Human gate timeout | Default reject after **24h** (per-project override via `review_config.review_timeout_seconds`) |
| Budget exhaustion | **Stage-boundary hard stop**: current stage finishes, next stage skipped, run paused, human notified |
| Budget projected overrun mid-stage | Allow stage to finish (atomicity); pause before next stage; human approves raise |
| Worktree corruption | Cleanup + recreate from base sha |
| CrewAI Flow state loss | restore_from_state_id from `runs.flow_uuid`; stages re-check `completed_stages` |

Retry policy per stage:
```
Attempt 1 fail → 30s → Attempt 2
Attempt 2 fail → 2min → Attempt 3
Attempt 3 fail → mark stage failed
  conditional stage (tester/security) → continue, note in artifact
  critical stage (planner/coder) → pause run, notify human
```

Audit log captures every action for compliance.

## 9. Testing & Deployment

### Testing

Three layers:
1. **Unit** — schemas, adapters (mocked HTTP), gateway logic, idempotency key generation, budget enforcement
2. **Integration** — real Postgres/Redis (testcontainers), real adapters against VCR fixtures, stage crews with mocked LLM, full happy path, resume after kill
3. **Eval** — prompt-response cache (OpenHands pattern): LLM calls intercepted from stored pairs. CI runs deterministic + cheap. Prompt changes regenerate cache against real model on schedule

**Eval baseline (SWE-bench Verified subset):** Seed 10 Python issues across Django, Flask, Requests, scikit-learn, sympy, matplotlib, astropy, xarray, pvlib, and one smaller utility. Store expected patch + test outcome. Each code change in LLM prompts/agent logic must preserve or improve eval score.

### Deployment

```yaml
# docker-compose.yml (production)
services:
  api:
    build: .
    command: uvicorn app.main:app --workers 4
  worker:
    build: .
    command: python -m app.worker
    deploy: { replicas: 2 }
  postgres:
    image: postgres:16
  redis:
    image: redis:7-alpine
```

API stateless, workers scale based on Redis queue depth. Postgres + Redis managed (RDS/ElastiCache in prod).

### Observability

- OpenTelemetry traces (every LLM call, adapter call, stage transition)
- Structured JSON logs (stage, run_id, actor)
- Prometheus metrics: `stage_duration_seconds{stage}`, `llm_tokens_total{provider,tier}`, `runs_in_progress`, `runs_failed_total`, `budget_spent_usd{project}`
- Grafana dashboard JSON provided

### Configuration

`.env` keys:
- `DATABASE_URL`, `REDIS_URL`
- `TASK_SOURCE_TYPE` + provider-specific creds
- `REVIEW_SURFACE_TYPE` + provider-specific creds
- `LLM_ENDPOINTS` (JSON list — see §5.7)
- `GIT_CREDENTIALS_VAULT_PATH` (encrypted at rest)

## 10. Resolved Decisions

(Replaces prior "Open Questions". All 12 gaps from review filled.)

| # | Topic | Decision |
|---|---|---|
| 1 | LLM cost cap | Per-project monthly budget (USD), hard stop |
| 1a | Stop granularity | Stage boundary (current stage finishes, next paused) |
| 2 | Multi-tenancy | Self-host single-org, row-level `project_id` isolation |
| 3 | Review timeout | Default 24h, per-project override via `review_config.review_timeout_seconds` |
| 4 | Eval baseline | SWE-bench Verified subset, 10 Python issues |
| 5 | Worktree storage | Local disk (`/var/worktrees/`), 30-day TTL |
| 6 | Rate-limit | Per-provider global (token-bucket in Redis) |
| 7 | CrewAI Flow | **Spike first**, then spec refined based on findings |
| 8 | Tier → model | OpenAI/Anthropic-compatible endpoints, configured per endpoint in `LLMConfig` |
| 9 | Code layout | `src/` layout + domain-by-module |
| 10 | DB schema | Full SQL DDL in §7 |
| 11 | API auth | API-key only (`X-API-Key` header) |
| 12 | Build order | Vertical slice (end-to-end happy path first) |

## 11. Implementation Milestones (Q12)

Vertical slice approach. Each milestone produces a usable, demoable increment.

- **M0 — Foundation (1 week)**
  - `src/` skeleton (per §9 layout decision)
  - Postgres + Redis via docker-compose
  - Alembic baseline migration (all tables in §7)
  - FastAPI skeleton with `/health`, `/ready`, API-key middleware
  - CI: lint + unit tests

- **M1 — CrewAI Flow Spike (Q7) (1 week)**
  - Minimal POC: 3 stages, kill mid-run, resume, idempotency on 1 fake external call
  - Refine §5.2 / §6 based on findings
  - Document any spec changes

- **M2 — Vertical Slice: Happy Path (2 weeks)**
  - 1 TaskSourceAdapter: CSV (simplest, no auth)
  - 1 LLMEndpoint: local Ollama (OpenAI-compatible)
  - 2 stages: Planner + Coder (skip tester/security/reviewer/docs for now)
  - 1 ReviewSurfaceAdapter: WebUiAdapter (no external dep)
  - End-to-end: POST execute → plan → code → "review" → mark done
  - Budget guard at stage boundary

- **M3 — Full Pipeline (3 weeks)**
  - Remaining stages: Tester, Security, Reviewer, Docs
  - Parallel join (`and_(tester, security)`) + conditional routing
  - Idempotency receipts for all external calls
  - Audit log

- **M4 — Real Adapters (3 weeks)**
  - GitHub Issues + GitHub PR (most common combo)
  - Jira Cloud REST + Jira comment
  - Linear GraphQL
  - Slack Block Kit
  - Webhooks

- **M5 — Suggestion Engine + Eval (2 weeks)**
  - SuggestionEngine wired to repo + TaskSourceAdapter
  - Web UI for picking proposals
  - SWE-bench eval seed (10 issues)
  - Eval runner integrated in CI

- **M6 — Polish (2 weeks)**
  - OpenTelemetry traces
  - Prometheus metrics + Grafana JSON
  - Rate limiter, circuit breaker, fallback chain hardening
  - Worktree sweeper task
  - Multi-key round-robin

- **M7 — Hardening (1 week)**
  - Load test, chaos test (kill Postgres mid-run, kill Redis, etc.)
  - Docs site + onboarding playbook
  - Security review

## 12. References

- CrewAI Flow docs: state persistence, `restore_from_state_id`, `checkpoint=True`
- SWE-agent ACI paper: agent-computer interface design
- OpenHands paper: event-stream architecture, multi-agent delegation, agent integration tests
- Devin docs: interactive planning, Agentic MapReduce, PR review patterns
- mazori-ai/llm_gateway: tier abstraction, circuit breaker, fallback chain
- eng-mostafa-alrahal/langgraph-agent-clean-architecture: clean architecture reference (FastAPI + Postgres + Redis + LangGraph)
- Atlassian Rovo MCP: official Jira/Confluence integration protocol
- SWE-bench Verified: https://www.swebench.com/