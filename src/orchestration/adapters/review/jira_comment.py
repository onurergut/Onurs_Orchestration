"""Jira comment review surface adapter.

Implements ``ReviewSurfaceAdapter`` against a Jira Cloud issue's comment
thread. ``notify_review`` posts a comment summarising the artifacts and
records a receipt so subsequent calls can resolve the issue key. The
``await_decision`` loop polls ``/issue/{key}/comment`` for an
approve/reject keyword (case-insensitive substring match); the first
matching comment wins. A timeout defaults to a rejection per spec.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.adapters.review.base import (
    Artifacts,
    Decision,
    Receipt,
    ReviewSurfaceAdapter,
)
from orchestration.adapters.task_source.jira import JiraAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.run import Run

# Default between-poll sleep for ``await_decision``.
_POLL_INTERVAL_S = 5.0

# Decision keyword sets. Substring match (case-insensitive) on the comment body.
_APPROVE_KEYWORDS = ("approve", "lgtm", "approved")
_REJECT_KEYWORDS = ("reject", "denied")


class JiraCommentAdapter(ReviewSurfaceAdapter):
    """Review surface backed by a Jira Cloud issue's comments."""

    name = "jira_comment"

    def __init__(
        self,
        email: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
        project_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self.email = email if email is not None else os.environ.get("JIRA_EMAIL")
        self.token = token if token is not None else os.environ.get("JIRA_API_TOKEN")
        self.base_url = (
            base_url if base_url is not None else os.environ.get("JIRA_BASE_URL")
        )
        self.project_key = (
            project_key
            if project_key is not None
            else os.environ.get("JIRA_PROJECT_KEY")
        )

        missing: list[str] = []
        if not self.email:
            missing.append("JIRA_EMAIL")
        if not self.token:
            missing.append("JIRA_API_TOKEN")
        if not self.base_url:
            missing.append("JIRA_BASE_URL")
        if not self.project_key:
            missing.append("JIRA_PROJECT_KEY")
        if missing:
            raise ValueError(
                f"JiraCommentAdapter requires env var(s): {', '.join(missing)}"
            )

        # Cast: mypy sees `self.base_url` as `str | None` per the attribute
        # type, but the `missing` check above already proved it's a non-empty str.
        self.base_url = self.base_url.rstrip("/")  # type: ignore[union-attr]
        self._client = client
        self._timeout = timeout
        self._poll_interval_s = poll_interval_s

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _classify_comment(body: str) -> Decision | None:
        """Map a comment body to a Decision, or None if no keyword is found.

        Approve keywords are checked first so a comment like "approved, but…"
        still wins as approval rather than being misclassified as anything else.
        """
        if not body:
            return None
        text = body.strip().lower()
        for kw in _APPROVE_KEYWORDS:
            if kw in text:
                return Decision(approve=True, reject=False, comment=body)
        for kw in _REJECT_KEYWORDS:
            if kw in text:
                return Decision(approve=False, reject=True, comment=body)
        return None

    @staticmethod
    def _build_review_body(artifacts: Artifacts) -> str:
        """Compose the human-readable comment body for ``notify_review``."""
        parts: list[str] = [
            "orchestration review for run_id: review requested",
            "",
            artifacts.summary or "",
        ]
        if artifacts.diff:
            parts.append("\nDiff:\n```diff\n" + artifacts.diff + "\n```")
        if artifacts.test_report:
            parts.append(
                "\nTest report:\n```json\n"
                + json.dumps(artifacts.test_report, indent=2)
                + "\n```"
            )
        parts.append(
            "\nReply with 'approve' (or 'lgtm' / 'approved') to approve, "
            "or 'reject' (or 'denied') to reject."
        )
        return "\n".join(parts).strip()

    @staticmethod
    def _extract_text(body: Any) -> str:
        """Pull plain text from a Jira comment body payload."""
        if isinstance(body, dict):
            return JiraAdapter._from_adf(body)
        if isinstance(body, str):
            return body
        return ""

    async def _resolve_project_id(
        self, session: AsyncSession, run_id: UUID
    ) -> UUID:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return UUID(str(run.project_id))

    async def _resolve_issue_key(self, session: AsyncSession, run_id: UUID) -> str:
        """Find the Jira issue key associated with ``run_id``."""
        receipt = (
            await session.execute(
                select(ExternalReceipt)
                .where(ExternalReceipt.run_id == run_id)
                .where(ExternalReceipt.adapter_type == "jira_comment")
                .where(ExternalReceipt.operation == "notify")
                .order_by(ExternalReceipt.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if receipt is None or not receipt.external_id:
            raise ValueError(
                f"no Jira comment receipt found for run {run_id}; "
                "call notify_review first"
            )
        return str(receipt.external_id)

    # ------------------------------------------------------------- ReviewSurfaceAdapter

    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt:
        """Post a review-summary comment and persist a receipt.

        The issue key is taken from the issue key embedded in the run's
        notification context: callers are expected to have already created
        the Jira issue (via ``JiraAdapter.create_task``) and stored it on the
        run's task. We resolve the issue key through the run's existing
        receipts if any, otherwise through the most recent Jira task-source
        receipt. We fall back to the run's task ``external_id`` only when
        no receipt has been written yet for this run.
        """
        receipt_id = uuid4()

        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            issue_key = await self._resolve_issue_key_for_notify(session, run_id)

        body = self._build_review_body(artifacts)
        await self._post_comment(issue_key, body)

        async with session_scope() as session:
            await session.execute(
                insert(ExternalReceipt).values(
                    id=receipt_id,
                    run_id=run_id,
                    idempotency_key=f"jira_comment:notify:{run_id}",
                    adapter_type="jira_comment",
                    operation="notify",
                    external_id=issue_key,
                    request_payload=artifacts.model_dump(),
                    response_payload={"issue_key": issue_key},
                    status="succeeded",
                )
            )
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="notify_review",
                    payload={
                        "adapter": "jira_comment",
                        "issue_key": issue_key,
                        "summary": artifacts.summary,
                    },
                )
            )
        return Receipt(id=receipt_id, external_id=issue_key)

    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision:
        """Poll issue comments until an approve/reject keyword appears or timeout."""
        if not receipt.external_id:
            return Decision(
                approve=False, reject=True, comment="missing receipt external_id"
            )
        issue_key = receipt.external_id
        path = f"/rest/api/3/issue/{issue_key}/comment"

        async def _poll_until_decision() -> Decision:
            seen_ids: set[str] = set()
            while True:
                data = await self._request("GET", path)
                comments = data.get("comments") if isinstance(data, dict) else None
                if not isinstance(comments, list):
                    comments = []
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    cid = str(comment.get("id") or "")
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    body_text = self._extract_text(comment.get("body"))
                    decision = self._classify_comment(body_text)
                    if decision is not None:
                        return decision
                await asyncio.sleep(self._poll_interval_s)

        try:
            return await asyncio.wait_for(
                _poll_until_decision(), timeout=timeout_s
            )
        except TimeoutError:
            return Decision(approve=False, reject=True, comment="timeout")

    async def post_comment(self, run_id: UUID, body: str) -> None:
        """Add a comment to the Jira issue associated with the run."""
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            issue_key = await self._resolve_issue_key(session, run_id)

        await self._post_comment(issue_key, body)

        async with session_scope() as session:
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="post_comment",
                    payload={
                        "adapter": "jira_comment",
                        "issue_key": issue_key,
                        "body": body,
                    },
                )
            )

    # ------------------------------------------------------- low-level Jira calls

    async def _resolve_issue_key_for_notify(
        self, session: AsyncSession, run_id: UUID
    ) -> str:
        """Find the issue key for ``notify_review``.

        The Jira task source adapter (``JiraAdapter``) creates issues but does
        not itself persist ``ExternalReceipt`` rows — it only updates the
        ``Task`` model with ``external_id`` = issue key. We therefore look up
        the run's task and use its ``external_id`` as the issue key. Falls
        back to a previously saved jira_comment receipt when present.
        """
        # First try an existing jira_comment receipt for this run (idempotent re-notify).
        receipt = (
            await session.execute(
                select(ExternalReceipt)
                .where(ExternalReceipt.run_id == run_id)
                .where(ExternalReceipt.adapter_type == "jira_comment")
                .where(ExternalReceipt.operation == "notify")
                .order_by(ExternalReceipt.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if receipt is not None and receipt.external_id:
            return str(receipt.external_id)

        # Otherwise read the task linked to the run.
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run {run_id} not found")
        from orchestration.storage.models.task import Task

        task = (
            await session.execute(
                select(Task).where(Task.id == run.task_id)
            )
        ).scalar_one_or_none()
        if task is None or not task.external_id:
            raise ValueError(
                f"no Jira issue key found for run {run_id}; "
                "ensure the JiraAdapter was used to create the task"
            )
        return str(task.external_id)

    async def _post_comment(self, issue_key: str, body: str) -> None:
        """Issue a POST against the issue's comment endpoint."""
        await self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            json={"body": JiraAdapter._to_adf(body)},
        )

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[Any]:
        """Issue an HTTP request against Jira and return parsed JSON.

        Duplicated from ``JiraAdapter`` so the review surface stays usable
        without an instantiated task-source adapter; future refactors can
        hoist this into a shared ``_JiraClient`` mixin.
        """
        # Cast: mypy sees `self.base_url` as `str | None`; the constructor
        # proves it's a non-empty str before we get here.
        base: str = self.base_url  # type: ignore[assignment]
        url = base + ("/" + path if not path.startswith("/") else path)
        raw = f"{self.email}:{self.token}".encode()
        encoded = base64.b64encode(raw).decode("ascii")
        headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "orchestration-jira-adapter",
        }
        extra_headers = kwargs.pop("headers", None)
        if extra_headers:
            headers.update(extra_headers)

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            if not response.content:
                return {}
            data: Any = response.json()
            if not isinstance(data, (dict, list)):
                raise TypeError(f"Unexpected JSON shape from {method} {url}")
            return data
        finally:
            if owns_client:
                await client.aclose()
