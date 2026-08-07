"""Audit log middleware for orchestrator stages.

Wraps each stage's body with enter/exit/error audit entries so every
stage transition is observable in the audit_log table. Failures inside
the wrapped block are reported as ``{stage}.error`` with the exception
class name and a truncated message (200 chars to avoid audit_log bloat),
and the original exception is re-raised so the flow's normal error path
is preserved.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from orchestration.storage.repositories import audit as audit_repo


@asynccontextmanager
async def stage_audit(
    project_id: UUID, run_id: UUID, stage: str
) -> AsyncIterator[dict]:
    """Emit ``stage:{stage}/enter`` then ``stage:{stage}/{exit|error}``.

    The yielded ``outcome`` dict is populated before returning so the
    caller can read ``status`` and ``duration_ms`` after the block.
    Errors hit the ``finally`` branch where we flip ``status`` to
    ``failure`` and stamp the exception's class name + truncated message
    before re-raising.
    """
    start = time.monotonic()
    await audit_repo.audit(project_id, run_id, f"stage:{stage}", "enter", {})
    outcome: dict = {"status": "success"}
    try:
        yield outcome
    except Exception as exc:
        outcome["status"] = "failure"
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        outcome["duration_ms"] = int((time.monotonic() - start) * 1000)
        action = "exit" if outcome["status"] == "success" else "error"
        await audit_repo.audit(project_id, run_id, f"stage:{stage}", action, outcome)
