# Orchestration System

Self-hosted multi-agent software engineering orchestrator.

Reads tasks from Jira/Linear/GitHub Issues, executes them through a resumable
7-stage agent pipeline (Planner → Coder → Tester ∥ Security → Reviewer → Docs →
Human Review), and submits results to GitHub PRs, Slack, or Jira comments.
LLM-agnostic — works with any OpenAI-compatible or Anthropic-compatible endpoint.

**293 tests, 1 skipped (live E2E).** Built in 6 milestones.

## Architecture

```
Task Source              Orchestrator              Review Surface
(Jira/Linear/GH/CSV)  →  CrewAI Flow 7-stage   →  (GH PR/Slack/Jira/Web)
                             ↓
                        LLM Gateway
                   (OpenAI / Anthropic compat)
                             ↓
                    Postgres + Redis
```

### 7-Stage Pipeline

| Stage | What it does | Mode |
|---|---|---|
| Planner | Reads repo + task, produces a step-by-step plan | LLM (THINKING tier) |
| Coder | Implements the plan, writes code + tests | LLM (FAST tier) |
| Tester | Runs `pytest`, parses JUnit XML, reports results | Subprocess |
| Security | Runs `bandit`, reports CWE findings | Subprocess |
| Reviewer | Reads `git diff`, emits structured review comments | LLM |
| Docs | Reads diff, proposes docs updates, commits | LLM |
| Human Review | Notifies via configured surface, awaits approve/reject | API call + polling |

Tester and Security run in **parallel**; Reviewer waits for both. Every stage is
guarded by `completed_stages` checks (idempotent on resume) and per-project
**budget enforcement** (hard stop at stage boundary). All stage entries/exits
are recorded in `audit_log`.

### Task Source Adapters

- **CSV** — local file, zero-config (dev/default)
- **GitHub Issues** — REST v3, PAT or App token
- **Jira** — Cloud REST v3, Basic auth, ADF format
- **Linear** — GraphQL API, API key

### Review Surface Adapters

- **Web UI** — internal review page (Redis pub/sub)
- **GitHub PR** — creates PR, polls comments for approve/reject
- **Jira Comment** — posts comment, polls for keywords
- **Slack** — Block Kit message with Approve/Reject buttons

### LLM Gateway

Pluggable endpoint system — configure any provider with an endpoint URL, API key,
model name, and protocol (`openai` or `anthropic`). Tier-aware (FAST/THINKING),
circuit breaker (closed/open/half-open, 5-failure threshold, 60s reset), per-provider
rate limiter (Redis-backed token bucket), and fallback chain.

Works with any OpenAI-compatible or Anthropic-compatible endpoint. End-to-end verified.

### Operational

- **Audit trail** — every stage entry/exit, external API call, and decision logged
- **Budget guard** — per-project monthly USD, hard stop at stage boundary
- **Idempotency** — every external side-effect keyed deterministically; retries/resumes never duplicate
- **Git worktrees** — per-task isolated worktree (`/var/worktrees/{uuid}`), 30-day TTL
- **Suggestion engine** — repo analysis + recent tasks → LLM proposals → picker API
- **SWE-bench eval** — seeded with 10 issues from the Verified subset, runner skeleton ready

## Observability

- **OpenTelemetry** — spans on every LLM call (`llm.endpoint`, `llm.model`, `llm.tier`)
- **Prometheus** — `/metrics` endpoint (stage duration, token counts, runs in progress, budget)
- **Structured logging** — `structlog` JSON output (via `LOG_LEVEL` in `.env`)

## Quickstart

```bash
cp .env.example .env   # fill in DATABASE_URL, REDIS_URL, LLM_* keys
make install           # pip install -e ".[dev]"
make docker-up         # postgres + redis
make migrate           # alembic upgrade head
make test              # ~294 tests
make run               # uvicorn on :8000
```

Manually run a pipeline:

```bash
make e2e-m2            # seeds a run row + executes planner → coder via LLM
```

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI, Pydantic v2, API-key auth |
| Pipeline | CrewAI Flow, 7 stage crews |
| LLM | OpenAI/Anthropic compat, circuit breaker, rate limiter |
| DB | PostgreSQL 16, SQLAlchemy 2.0 async, Alembic |
| Cache/Queue | Redis 7 (idempotency, rate limit, pub/sub) |
| Ops | OpenTelemetry, Prometheus, Docker Compose |
| Test | pytest (asyncio), httpx ASGI transport, VCR cassettes |
| Code quality | ruff, mypy (strict) |

## Configuration (`.env`)

```dotenv
# Database
DATABASE_URL=postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration
REDIS_URL=redis://localhost:6379/0

# Task source / review surface (one of: csv, github, jira, linear / web_ui, github_pr, jira_comment, slack)
TASK_SOURCE_TYPE=csv
REVIEW_SURFACE_TYPE=web_ui

# LLM endpoint (Anthropic-compatible example)
LLM_PROTOCOL=anthropic
LLM_ENDPOINT_URL=https://api.anthropic.com
LLM_API_KEY=sk-...
LLM_MODEL=claude-sonnet-4-5

# Auth
API_KEY_HEADER=X-API-Key

# Budget
DEFAULT_MONTHLY_BUDGET_USD=500

# Worktrees
WORKTREE_BASE_PATH=/var/worktrees
WORKTREE_TTL_DAYS=30

# Review
DEFAULT_REVIEW_TIMEOUT_SECONDS=86400
```

See `docs/superpowers/specs/2026-08-07-orchestration-design.md` for the full design.

## File Layout

```
src/orchestration/
├── api/              FastAPI routes + middleware (API-key auth, health, suggestions)
├── orchestrator/     CrewAI Flow state machine, budget guard
├── stages/           Planner, Coder, Tester, Security, Reviewer, Docs
├── adapters/
│   ├── task_source/  CSV, GitHub Issues, Jira, Linear
│   └── review/       Web UI, GitHub PR, Jira Comment, Slack
├── llm/              LLMEndpoint, OpenAI/Anthropic compat, router, circuit breaker, rate limit
├── storage/          SQLAlchemy models, async session, Redis pool, idempotency receipts
├── git/              per-task worktree manager
├── suggestion/       codebase analysis → proposal generator
├── workers/          orchestrator runner, worktree sweeper
└── observability/    OpenTelemetry, Prometheus metrics, audit middleware

tests/
├── api/              FastAPI route tests (httpx ASGI transport)
├── adapters/         adapter VCR cassette tests
├── git/              worktree manager tests
├── llm/              endpoint, rate limit, circuit breaker, fallback tests
├── orchestrator/     flow state + budget guard tests
├── stages/           planner, coder, tester, security, reviewer, docs tests
├── storage/          idempotency receipt tests
├── integration/      full-stack smoke tests
└── eval/             SWE-bench seed + runner
```

## Milestones

| Tag | What |
|---|---|
| `m0-foundation` | Scaffolding, Docker, FastAPI + Auth + DB + CI |
| `m2-vertical-slice` | End-to-end Planner+Coder via live LLM (E2E verified) |
| `m3-full-pipeline` | 7-stage pipeline + audit middleware |
| `m4-real-adapters` | GitHub, Jira, Linear, Slack (VCR cassette tests) |
| `m5-suggestion-and-eval` | SuggestionEngine + picker API + SWE-bench seed + runner |
| `m6-polish` | OTel, Prometheus, rate limiter, sweeper, circuit breaker |

## License

Self-hosted. No SaaS dependency.