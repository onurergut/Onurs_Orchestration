"""Prometheus metrics for the orchestration service.

Exposes the five metrics required by spec §9:

* ``stage_duration_seconds{stage}`` — histogram of stage execution time.
* ``llm_tokens_total{provider,tier}`` — counter of LLM tokens consumed.
* ``runs_in_progress`` — gauge of currently-running runs.
* ``runs_failed_total`` — counter of failed runs.
* ``budget_spent_usd{project}`` — counter of LLM cost accrued.

The ``render_metrics`` helper returns ``(body, content_type)`` so the
FastAPI route can hand it straight to ``Response(...)`` without
re-importing ``prometheus_client`` at the route layer. Tests reuse the
same helper to scrape the in-memory registry.

Test isolation: the default Prometheus client uses a process-global
registry. Module-level metrics are fine because all five are
increment-only or monotonic (no test should observe negative values),
and the smoke tests just assert the call works and the rendered
output contains the expected metric names.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Histogram of stage execution time, sliced by stage name. Buckets
# reflect the expected order of magnitude (sub-second to ~5 minutes)
# for planner / coder / tester / security / reviewer / docs stages.
STAGE_DURATION = Histogram(
    "stage_duration_seconds",
    "Time spent executing each orchestrator stage",
    labelnames=("stage",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

# Total LLM tokens consumed, sliced by provider and tier.
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed (prompt + completion)",
    labelnames=("provider", "tier"),
)

# Gauge of runs currently executing in worker(s).
RUNS_IN_PROGRESS = Gauge(
    "runs_in_progress",
    "Number of orchestrator runs currently in progress",
)

# Counter of runs that terminated in the failed state.
RUNS_FAILED = Counter(
    "runs_failed_total",
    "Total number of orchestrator runs that failed",
)

# Counter of LLM spend accumulated per project, in USD.
BUDGET_SPENT = Counter(
    "budget_spent_usd",
    "Total LLM spend accrued (USD) per project",
    labelnames=("project",),
)


def render_metrics() -> tuple[bytes, str]:
    """Render the current registry in Prometheus text format.

    Returns ``(body, content_type)`` so the FastAPI route can pass them
    directly to ``Response(content=body, media_type=content_type)``.
    """
    return generate_latest(), CONTENT_TYPE_LATEST


def observe_stage_duration(stage: str, seconds: float) -> None:
    """Record a stage's wall-clock duration in the histogram."""
    STAGE_DURATION.labels(stage=stage).observe(seconds)


def observe_llm_tokens(provider: str, tier: str, prompt: int, completion: int) -> None:
    """Increment the LLM token counter by ``prompt + completion``."""
    LLM_TOKENS.labels(provider=provider, tier=tier).inc(prompt + completion)


def inc_runs_in_progress() -> None:
    """Mark that one more run has started executing."""
    RUNS_IN_PROGRESS.inc()


def dec_runs_in_progress() -> None:
    """Mark that one run has finished executing (success or failure)."""
    RUNS_IN_PROGRESS.dec()


def inc_runs_failed() -> None:
    """Increment the failed-runs counter."""
    RUNS_FAILED.inc()


def observe_budget_spent(project: str, usd: float) -> None:
    """Add ``usd`` dollars to the project's accrued spend counter."""
    BUDGET_SPENT.labels(project=project).inc(usd)
