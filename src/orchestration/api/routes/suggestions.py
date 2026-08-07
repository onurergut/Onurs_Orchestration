"""Suggestion picker API routes.

Three endpoints that back the web-UI suggestion flow (UI itself lives in a
separate frontend repo):

    GET    /projects/{pid}/suggestions          - list pending suggestions
    GET    /projects/{pid}/suggestions/{sid}    - fetch one with proposals
    POST   /projects/{pid}/suggestions/{sid}/pick - user picked proposals
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.api.middleware.api_key import get_session, require_api_key
from orchestration.storage.models.project import Project
from orchestration.storage.models.suggestion import Suggestion
from orchestration.storage.models.task import Task
from orchestration.storage.models.user import User

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{pid}/suggestions",
    tags=["suggestions"],
)


# --- response shapes ---------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _suggestion_summary(s: Suggestion) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "status": s.status,
        "created_at": _iso(s.created_at),
        "proposal_count": len(s.proposals or []),
    }


def _suggestion_detail(s: Suggestion) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "project_id": str(s.project_id),
        "status": s.status,
        "created_at": _iso(s.created_at),
        "decided_at": _iso(s.decided_at),
        "proposals": list(s.proposals or []),
    }


# --- request models ----------------------------------------------------------


class PickItem(BaseModel):
    """Index of a proposal within the suggestion's ``proposals`` array."""

    index: int = Field(ge=0)


class SuggestionPickRequest(BaseModel):
    picks: list[PickItem] = Field(min_length=1)
    auto_execute: bool = False


# --- helpers -----------------------------------------------------------------


async def _get_project(session: AsyncSession, pid: UUID) -> Project:
    project = await session.get(Project, pid)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return project


async def _get_suggestion(
    session: AsyncSession, pid: UUID, sid: UUID
) -> Suggestion:
    s = await session.get(Suggestion, sid)
    if s is None or s.project_id != pid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="suggestion not found"
        )
    return s


def _external_id(sid: UUID, idx: int) -> str:
    # Stable, unique per (suggestion, proposal_index); fits the
    # (source_type, external_id) uniqueness constraint on ``tasks``.
    return f"suggestion:{sid}:{idx}"


# --- routes ------------------------------------------------------------------


@router.get("")
async def list_suggestions(
    pid: UUID = Path(...),
    _user: User = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return all pending suggestions for ``pid``, newest first."""
    await _get_project(session, pid)
    stmt = (
        select(Suggestion)
        .where(Suggestion.project_id == pid, Suggestion.status == "pending")
        .order_by(Suggestion.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {"suggestions": [_suggestion_summary(r) for r in rows]}


@router.get("/{sid}")
async def get_suggestion(
    pid: UUID = Path(...),
    sid: UUID = Path(...),
    _user: User = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a single suggestion with full proposal details."""
    s = await _get_suggestion(session, pid, sid)
    return _suggestion_detail(s)


@router.post("/{sid}/pick", status_code=status.HTTP_200_OK)
async def pick_suggestion(
    body: SuggestionPickRequest,
    pid: UUID = Path(...),
    sid: UUID = Path(...),
    _user: User = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Mark the suggestion as picked and create a Task for each picked proposal.

    Behaviour:
      * Validates the suggestion belongs to ``pid`` and is still ``pending``.
      * Validates each pick index against the proposal list length.
      * Creates one Task per pick (source_type='csv', stable external_id derived
        from ``(suggestion_id, index)``) so re-picking the same proposal yields
        the same external_id and is a no-op.
      * Sets the suggestion to ``status='picked'`` with ``decided_at=now()``.
      * If ``auto_execute`` is true, enqueues an orchestrator job for each new
        task. The real queue lives in M7; for now we log a structured line so
        callers and tests can observe the intent.
    """
    suggestion = await _get_suggestion(session, pid, sid)

    if suggestion.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"suggestion already {suggestion.status}",
        )

    proposals: list[dict[str, Any]] = list(suggestion.proposals or [])
    if not proposals:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="suggestion has no proposals",
        )

    # Deduplicate + sort indices to keep behaviour deterministic.
    seen_idx: set[int] = set()
    indices: list[int] = []
    for p in body.picks:
        if p.index in seen_idx:
            continue
        seen_idx.add(p.index)
        indices.append(p.index)

    for idx in indices:
        if idx >= len(proposals):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"proposal index {idx} out of range (have {len(proposals)})",
            )

    # Verify project exists (also covers the FK on tasks).
    await _get_project(session, pid)

    task_ids: list[str] = []
    would_execute: list[str] = []
    for idx in indices:
        proposal = proposals[idx]
        title = str(proposal.get("title", "")).strip() or f"Suggestion {sid} #{idx}"
        description = proposal.get("description") or proposal.get("rationale") or ""
        metadata = {
            "source": "suggestion",
            "suggestion_id": str(sid),
            "proposal_index": idx,
            "estimated_complexity": proposal.get("estimated_complexity"),
            "related_files": proposal.get("related_files", []),
            "rationale": proposal.get("rationale"),
        }
        task = Task(
            id=uuid4(),
            project_id=pid,
            external_id=_external_id(sid, idx),
            source_type="csv",
            title=title,
            description=description,
            status="pending",
            task_metadata=metadata,
        )
        session.add(task)
        await session.flush()
        task_ids.append(str(task.id))
        if body.auto_execute:
            would_execute.append(str(task.id))

    suggestion.status = "picked"
    suggestion.decided_at = datetime.now(UTC)

    # ``get_session`` does not commit on exit; persist here so the status flip
    # and the new Task rows survive past the request.
    await session.commit()

    if would_execute:
        # Real queue wiring is M7; emit a single structured log line so the
        # intent is observable from tests and ops without blocking the API.
        log.info(
            "auto_execute requested for suggestion",
            extra={
                "suggestion_id": str(sid),
                "project_id": str(pid),
                "task_ids": would_execute,
            },
        )

    return {
        "picked_count": len(task_ids),
        "task_ids": task_ids,
        "auto_execute": body.auto_execute,
    }
