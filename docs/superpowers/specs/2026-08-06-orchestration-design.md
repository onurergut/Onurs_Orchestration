# Orchestration System — Design Spec

**Date:** 2026-08-06
**Status:** Approved (architecture), pending implementation
**Author:** Brainstorming session with user

## 1. Overview

A self-hosted backend service that orchestrates multi-agent software engineering workflows. The system integrates with task management systems (Jira/Linear/GitHub Issues), suggests new work from codebase analysis, executes tasks through a 7-stage agent pipeline, and submits results to a pluggable human review surface.

**Target users:** Engineering teams with code IP concerns, multi-LLM strategies, Jira/Linear workflows, and audit/compliance requirements.

**Stack:** Python 3.12, FastAPI, CrewAI Flow, Pydantic v2, SQLAlchemy 2.0 async, PostgreSQL 16, Redis 7, Docker Compose. LLM-agnostic (Anthropic, OpenAI, Google, Mistral, Ollama).

## 2. Goals

- Integrate with task management systems without vendor lock-in (pluggable adapter)
- Execute tasks via explicit, resumable 7-stage pipeline (Planner → Coder → Tester → Reviewer → Security → Docs → Human)
- Suggest new tasks from codebase + historical patterns (on-demand)
- Submit completed work to a pluggable review surface (GitHub PR, Slack, Jira, Web)
- Persist audit trail (every action, every external side-effect)
- Allow any LLM provider via gateway (tier-aware, circuit breaker, fallback)
- Self-host with Docker Compose, no SaaS dependency

## 3. Non-Goals (v1)

- Trajectory learning from past runs (v2)
- Agentic MapReduce for whole-codebase scans (v2)
- Fleet of parallel sub-agents (v3)
- Full Devin-Review-style PR review tool (basic reviewer stage only in v1)
- Multi-tenant SaaS billing (self-hosted per org)
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
│  │  HTTP API (FastAPI)                              │       │
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
│  │  Tier (FAST|THINKING) + circuit breaker + fallback│       │
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

### Orchestrator refinements (from research)

- **Two-tier persistence:** `@persist` saves FlowState (artifacts, plan content); `checkpoint=True` saves execution pointer (`completed_stages`). Flow always re-enters at `@start`, but stages check `completed_stages` and skip if done. Resume via `restore_from_state_id`.
- **Stage atomicity:** Pause boundaries only between stages. A stage runs to completion or fails atomically — never half-done. Within a stage, retries re-execute the whole stage; idempotency on side-effects prevents duplicates from those retries.
- **Idempotency on side-effects:** Every external call uses a deterministic `idempotency_key`. On resume or stage retry, the adapter checks for an existing receipt with that key; if found, it returns the existing record and skips re-execution. Prevents duplicate Jira tickets, PRs, Slack messages on retry/resume.
- **Conditional routing:** `@router` after each stage. Test fail → coder (max 3 loops). Security findings high → reviewer flagged. Linear pipeline is a default, not the only path.
- **LLM gateway:** All LLM calls go through a gateway that resolves tier to provider-specific model, applies circuit breaker, fallback chain, per-tenant rate limit, and multi-key pool round-robin. Unified response shape regardless of provider.

## 5. Components

### 5.1 HTTP API (`app/api/`)

Thin entrypoint. Validate, queue, respond. No state.

```
POST   /projects/{pid}/tasks/suggest
POST   /projects/{pid}/tasks/{tid}/execute
GET    /runs/{rid}
POST   /runs/{rid}/approve | reject | comment
GET    /projects/{pid}/runs
POST   /webhooks/{adapter}
GET    /health, /ready
```

JWT or API-key auth, per-org scoping. Idempotency-Key header supported. OpenAPI auto-generated.

### 5.2 Orchestrator (`app/orchestrator/`)

CrewAI Flow state machine. Determines next stage, handles resume, routes conditionally.

```python
class OrchestratorFlow(Flow[OrchestratorState]):
    @start()
    def load_context(self): ...
    @listen(load_context)
    def planner_stage(self): ...
    @router(planner_stage)
    def route_after_planner(self) -> str: ...
    @listen(route_after_planner)
    def coder_stage(self): ...
    # tester_stage + security_stage run in parallel after coder
    @listen(and_(tester_stage, security_stage))  # join
    def reviewer_stage(self): ...
    # ... docs, human_review
```

State:
```python
class OrchestratorState(BaseModel):
    run_id: UUID
    flow_uuid: UUID
    project_id: UUID
    task_id: str
    completed_stages: set[str]
    stage_artifacts: dict[str, Artifact]
    external_receipts: dict[str, Receipt]
    decision: Decision | None
```

### 5.3 Stage Crews (`app/stages/`)

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

### 5.4 Task Source Adapter (`app/adapters/task_source/`)

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

### 5.5 Review Surface Adapter (`app/adapters/review/`)

Human-in-the-loop channel.

```python
class ReviewSurfaceAdapter(ABC):
    async def notify_review(self, run: Run, artifacts: Artifacts) -> Receipt: ...
    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision: ...
    async def post_comment(self, receipt: Receipt, body: str) -> None: ...
```

Implementations: `GitHubPrAdapter` (branch push + PR + check status), `JiraCommentAdapter` (transition + comment + watcher), `SlackAdapter` (Block Kit with buttons), `WebUiAdapter` (internal review page).

### 5.6 Suggestion Engine (`app/suggestion/`)

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

### 5.7 LLM Provider Gateway (`app/llm/`)

```python
class LLMClient(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def complete_structured(self, request: CompletionRequest, schema: type[T]) -> T: ...
```

Internal:
- `ProviderRegistry` — name → provider
- `ProviderRouter` — strategies: capability_first, cost_first, latency_first
- `CircuitBreaker` — closed/open/half-open (threshold 5, timeout 60s)
- `FallbackChain` — primary fail → next provider
- `MultiKeyPool` — round-robin per provider (billing separation)
- Unified response shape (provider-agnostic)

Tier abstraction: `FAST | THINKING`. Planner THINKING, Coder FAST by default. Per-project override in `LLMConfig`.

### 5.8 Storage (`app/storage/`)

Postgres (state) + Redis (queue/cache/sessions).

Postgres tables: `projects`, `tasks`, `runs`, `stage_artifacts`, `external_receipts`, `audit_log`, `suggestions`, `llm_configs`, `users`. See Section 7 for full schema.

Redis keys: `idempotency:{key}`, `ratelimit:{tenant}:{provider}`, `circuit:{provider}`, `webhook_pending:{receipt_id}`, `session:{run_id}`.

### 5.9 Git Worktree Manager (`app/git/`)

```python
class WorktreeManager:
    async def create(self, project_id: UUID, task_id: str) -> Worktree: ...
    async def diff(self, worktree_id: UUID) -> Diff: ...
    async def commit(self, worktree_id: UUID, message: str) -> Commit: ...
    async def push_branch(self, worktree_id: UUID) -> BranchRef: ...
    async def cleanup(self, worktree_id: UUID) -> None: ...
```

Per-task isolated worktree: `git worktree add -b orch/{task_id} /var/worktrees/{id} main`. Each stage runs in that path. Diff snapshot saved as artifact. Branch pushed at final stage (PR adapter handles PR creation).

## 6. Data Flow

### Path 1: Execute task

1. POST execute → API validates, writes Run row, enqueues orchestrator.run
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
14. timeout → default reject

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

## 7. Data Model

### Idempotency key conventions

```
jira_create_task:    f"task:{project_id}:{task_id}:create"
jira_comment:        f"comment:{project_id}:{task_id}:{stage}:{attempt}"
github_pr_create:    f"pr:{project_id}:{task_id}:create"
github_pr_comment:   f"pr_comment:{project_id}:{task_id}:{stage}"
slack_msg:           f"slack:{project_id}:{task_id}:notify"
git_branch_push:     f"branch_push:{project_id}:{task_id}"
```

### Key fields per table

- `runs.idempotency_key` UNIQUE — for POST execute
- `external_receipts.idempotency_key` UNIQUE — for all external calls
- `external_receipts.external_id` — Jira KEY, PR number, Slack ts, etc.
- `audit_log` — append-only, BIGSERIAL, indexed by (run_id, ts)

Full table definitions in implementation plan.

## 8. Error Handling

| Failure | Handling |
|---|---|
| LLM transient (rate limit, 5xx, timeout) | Circuit breaker → fallback. Retry 3x exponential |
| LLM quality (schema parse fail, low confidence) | Retry 2x with feedback. Then escalate to human |
| Tool/sandbox (git push fail, test fail) | Stage retry 3x. Then mark stage failed or escalate |
| External API (Jira 503, Slack down) | Adapter retry. Receipt idempotency → no dup |
| Side-effect dup on resume | Adapter checks idempotency_key, returns existing receipt |
| Stage fatal (Planner can't make plan) | Run.status=failed. Task stays "blocked" in Jira |
| Human gate timeout | Default reject after 24h |
| Budget exhaustion | Pause run, notify human |
| Worktree corruption | Cleanup + recreate from base sha |

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
1. **Unit** — schemas, adapters (mocked HTTP), gateway logic, idempotency key generation
2. **Integration** — real Postgres/Redis (testcontainers), real adapters against VCR fixtures, stage crews with mocked LLM, full happy path, resume after kill
3. **Eval** — prompt-response cache (OpenHands pattern): LLM calls intercepted from stored pairs. CI runs deterministic + cheap. Prompt changes regenerate cache against real model on schedule

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
- Prometheus metrics: `stage_duration_seconds{stage}`, `llm_tokens_total{provider,tier}`, `runs_in_progress`, `runs_failed_total`
- Grafana dashboard JSON provided

### Configuration

`.env` keys:
- `DATABASE_URL`, `REDIS_URL`
- `TASK_SOURCE_TYPE` + provider-specific creds
- `REVIEW_SURFACE_TYPE` + provider-specific creds
- LLM: `ANTHROPIC_API_KEYS`, `OPENAI_API_KEY`, `DEFAULT_TIER`, `FALLBACK_CHAIN`
- `GIT_CREDENTIALS_VAULT_PATH` (encrypted at rest)

## 10. Open Questions

- **LLM cost caps:** Per-project monthly budget? Hard stop vs soft warning?
- **Multi-tenancy:** Single org per deployment (self-host model), or SaaS multi-tenant with row-level isolation?
- **Eval baseline:** Which golden tasks to seed for v1? Need 5–10 small repos with known issues + expected outputs.
- **Rate-limit granularity:** Per-org, per-user, or per-project?
- **Worktree storage:** Local disk or NFS/S3-backed? Cleanup TTL (7 days? 30?)
- **Review timeout default:** 24h feels long. 4h? Configurable per project?

These resolve during implementation planning. Default answers will be chosen unless explicitly overridden.

## 11. References

- CrewAI Flow docs: state persistence, `restore_from_state_id`, `checkpoint=True`
- SWE-agent ACI paper: agent-computer interface design
- OpenHands paper: event-stream architecture, multi-agent delegation, agent integration tests
- Devin docs: interactive planning, Agentic MapReduce, PR review patterns
- mazori-ai/llm_gateway: tier abstraction, circuit breaker, fallback chain
- eng-mostafa-alrahal/langgraph-agent-clean-architecture: clean architecture reference (FastAPI + Postgres + Redis + LangGraph)
- Atlassian Rovo MCP: official Jira/Confluence integration protocol
