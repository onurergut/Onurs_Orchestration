"""Tests for the Prometheus metrics module (M6/Task 38).

The metrics module exposes five series required by spec §9:

* ``stage_duration_seconds`` (Histogram, label: stage)
* ``llm_tokens_total`` (Counter, labels: provider, tier)
* ``runs_in_progress`` (Gauge)
* ``runs_failed_total`` (Counter)
* ``budget_spent_usd`` (Counter, label: project)

The metric objects are module-level globals, so all tests share the
same in-process registry. Each test asserts by scraping
``render_metrics()`` and parsing the text exposition format — that's
what the FastAPI ``/metrics`` route does too, so we're exercising the
same code path as the scrape endpoint.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestration.observability.metrics import (
    BUDGET_SPENT,
    LLM_TOKENS,
    RUNS_FAILED,
    RUNS_IN_PROGRESS,
    STAGE_DURATION,
    dec_runs_in_progress,
    inc_runs_failed,
    inc_runs_in_progress,
    observe_budget_spent,
    observe_llm_tokens,
    observe_stage_duration,
    render_metrics,
)


def _exposition_text() -> str:
    """Return the current Prometheus exposition body as a string."""
    body, _ = render_metrics()
    return body.decode("utf-8")


def test_render_metrics_returns_bytes_and_content_type() -> None:
    """``render_metrics`` returns a ``(bytes, str)`` pair.

    The bytes payload must be the Prometheus text exposition format
    (the canonical header is ``# HELP`` lines), and the content type
    must be the one Prometheus scrapers expect.
    """
    body, content_type = render_metrics()
    assert isinstance(body, bytes)
    # Every Prometheus exposition body starts with a HELP line.
    assert body.startswith(b"# HELP")
    # Content-Type matches the prometheus_client constant.
    from prometheus_client import CONTENT_TYPE_LATEST

    assert content_type == CONTENT_TYPE_LATEST
    assert "text/plain" in content_type


def test_all_five_metrics_are_exposed() -> None:
    """Scraping the registry shows every required metric name."""
    text = _exposition_text()
    for name in (
        "stage_duration_seconds",
        "llm_tokens_total",
        "runs_in_progress",
        "runs_failed_total",
        "budget_spent_usd",
    ):
        assert name in text, f"missing metric {name!r} in exposition"


def test_observe_stage_duration_increments_histogram() -> None:
    """Calling ``observe_stage_duration`` adds to the histogram count.

    The Prometheus client exposes ``_sum`` on the child label set
    (private but stable since 0.20). For count we parse the
    exposition output — ``stage_duration_seconds_count{stage=...}``
    is the canonical scrape line for ``Histogram.count``.
    """
    import re

    label = "test_hist_test_stage"
    sum_before = STAGE_DURATION.labels(stage=label)._sum.get()  # type: ignore[attr-defined]

    def _count_for(stage: str, text: str) -> float:
        # Match the ``_count`` line for our label set and parse the value.
        pattern = rf'stage_duration_seconds_count\{{stage="{re.escape(stage)}"\}}\s+([0-9eE+\-.]+)'
        m = re.search(pattern, text)
        assert m is not None, f"no _count line for stage={stage!r}"
        return float(m.group(1))

    count_before = _count_for(label, _exposition_text())

    observe_stage_duration(label, 0.42)
    observe_stage_duration(label, 1.5)

    sum_after = STAGE_DURATION.labels(stage=label)._sum.get()  # type: ignore[attr-defined]
    count_after = _count_for(label, _exposition_text())

    assert count_after - count_before == 2
    assert sum_after - sum_before == pytest.approx(0.42 + 1.5, rel=1e-9)

    # The histogram label appears in the scraped exposition.
    text = _exposition_text()
    assert f'stage_duration_seconds_count{{stage="{label}"}}' in text
    assert 'stage_duration_seconds_bucket' in text


def test_observe_llm_tokens_increments_counter() -> None:
    """``observe_llm_tokens`` adds ``prompt + completion`` to the counter."""
    label_pair = ("test_provider", "test_tier")
    value_before = LLM_TOKENS.labels(*label_pair)._value.get()  # type: ignore[attr-defined]

    observe_llm_tokens("test_provider", "test_tier", prompt=100, completion=50)
    observe_llm_tokens("test_provider", "test_tier", prompt=10, completion=5)

    value_after = LLM_TOKENS.labels(*label_pair)._value.get()  # type: ignore[attr-defined]
    assert value_after - value_before == 165

    # The labelled series appears in the exposition.
    text = _exposition_text()
    assert 'llm_tokens_total{provider="test_provider",tier="test_tier"}' in text


def test_runs_in_progress_inc_and_dec() -> None:
    """``inc_runs_in_progress`` / ``dec_runs_in_progress`` move the gauge."""
    before = RUNS_IN_PROGRESS._value.get()  # type: ignore[attr-defined]
    inc_runs_in_progress()
    inc_runs_in_progress()
    mid = RUNS_IN_PROGRESS._value.get()  # type: ignore[attr-defined]
    assert mid - before == 2

    dec_runs_in_progress()
    after = RUNS_IN_PROGRESS._value.get()  # type: ignore[attr-defined]
    assert after - before == 1

    text = _exposition_text()
    assert "runs_in_progress" in text


def test_inc_runs_failed_increments_counter() -> None:
    """Each ``inc_runs_failed`` call moves the counter up by one."""
    before = RUNS_FAILED._value.get()  # type: ignore[attr-defined]

    inc_runs_failed()
    inc_runs_failed()
    inc_runs_failed()

    after = RUNS_FAILED._value.get()  # type: ignore[attr-defined]
    assert after - before == 3

    text = _exposition_text()
    assert "runs_failed_total" in text


def test_observe_budget_spent_increments_usd_counter() -> None:
    """``observe_budget_spent`` accumulates USD per project."""
    project = "test_project"
    before = BUDGET_SPENT.labels(project=project)._value.get()  # type: ignore[attr-defined]

    observe_budget_spent(project, 0.05)
    observe_budget_spent(project, 0.025)

    after = BUDGET_SPENT.labels(project=project)._value.get()  # type: ignore[attr-defined]
    assert after - before == pytest.approx(0.075, rel=1e-9)

    text = _exposition_text()
    assert f'budget_spent_usd_total{{project="{project}"}}' in text


@pytest.mark.asyncio
async def test_metrics_route_serves_prometheus_text() -> None:
    """The ``GET /metrics`` endpoint returns the Prometheus exposition body.

    We mount a fresh FastAPI app so the test doesn't depend on the
    production ``create_app()`` (which pulls in DB / Redis / auth).
    Hitting the route exercises the same handler the API server uses.
    """
    app = FastAPI()

    @app.get("/metrics", include_in_schema=False)
    async def metrics_route():
        from fastapi import Response

        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Body is the Prometheus exposition — non-empty and starts with a HELP line.
    assert r.content.startswith(b"# HELP")
    # And it contains at least one of our metric names.
    assert b"runs_in_progress" in r.content
