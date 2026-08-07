"""GitHub PR review surface adapter.

Opens a pull request on ``orch/{run_id}`` (per spec §5.9) for human review and
polls the PR's comment thread for an approve/reject keyword. Decisions are
derived from comment text only — a comment containing "approve", "lgtm", or
"approved" (case-insensitive) counts as approval; one containing "reject" or
"denied" counts as rejection. The first matching comment wins.
"""

from __future__ import annotations

import asyncio
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
from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.run import Run

# GitHub limits PR bodies to ~65,535 chars; stay safely under that.
_MAX_PR_BODY_CHARS = 60_000

# Default between-poll sleep for `await_decision`.
_POLL_INTERVAL_S = 5.0

# Decision keyword sets. Substring match (case-insensitive) on the comment body.
_APPROVE_KEYWORDS = ("approve", "lgtm", "approved")
_REJECT_KEYWORDS = ("reject", "denied")


class GitHubPrAdapter(ReviewSurfaceAdapter):
    """Review surface backed by a GitHub pull request."""

    name = "github_pr"

    # GitHub REST API v3 base URL.
    API_BASE = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        poll_interval_s: float = _POLL_INTERVAL_S,
        base_branch: str = "main",
    ) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.repo = repo if repo is not None else os.environ.get("GITHUB_REPO")

        missing: list[str] = []
        if not self.token:
            missing.append("GITHUB_TOKEN")
        if not self.repo:
            missing.append("GITHUB_REPO")
        if missing:
            raise ValueError(
                f"GitHubPrAdapter requires env var(s): {', '.join(missing)}"
            )

        self._client = client
        self._timeout = timeout
        self._poll_interval_s = poll_interval_s
        self._base_branch = base_branch

    # ------------------------------------------------------------------ helpers

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "orchestration-github-adapter",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.API_BASE}{path}"

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[Any]:
        """Issue an HTTP request against the GitHub API and return parsed JSON."""
        url = self._url(path)
        headers = self._headers()
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

    @staticmethod
    def _build_pr_body(artifacts: Artifacts) -> str:
        """Compose a PR description from summary, diff, and test report."""
        parts: list[str] = [artifacts.summary or ""]
        if artifacts.diff:
            parts.append("\n## Diff\n```diff\n" + artifacts.diff + "\n```")
        if artifacts.test_report:
            parts.append(
                "\n## Test report\n```json\n"
                + json.dumps(artifacts.test_report, indent=2)
                + "\n```"
            )
        body = "\n".join(parts).strip()
        if len(body) > _MAX_PR_BODY_CHARS:
            body = body[:_MAX_PR_BODY_CHARS] + "\n\n... (truncated)"
        return body

    @staticmethod
    def _classify_comment(body: str) -> Decision | None:
        """Map a comment body to a Decision, or None if no keyword is found.

        Approve keywords are checked first so a comment like "approved, but…" still
        wins as approval rather than being misclassified.
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

    async def _resolve_project_id(
        self, session: AsyncSession, run_id: UUID
    ) -> UUID:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return UUID(str(run.project_id))

    # ------------------------------------------------------------- ReviewSurfaceAdapter

    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt:
        """Open a pull request for the run and persist the receipt."""
        receipt_id = uuid4()
        head_branch = f"orch/{run_id}"
        body = self._build_pr_body(artifacts)
        title = f"orchestration run {run_id}"

        # Resolve project_id up front so we fail fast on unknown runs and
        # avoid making an HTTP call when the input is clearly invalid.
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)

        data = await self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={
                "title": title,
                "head": head_branch,
                "base": self._base_branch,
                "body": body,
            },
        )
        if not isinstance(data, dict):
            raise TypeError("Unexpected response shape when creating PR")
        pr_number = str(data.get("number") or "")
        if not pr_number:
            raise ValueError("GitHub PR create response missing 'number'")

        async with session_scope() as session:
            await session.execute(
                insert(ExternalReceipt).values(
                    id=receipt_id,
                    run_id=run_id,
                    idempotency_key=f"github_pr:notify:{run_id}",
                    adapter_type="github_pr",
                    operation="notify",
                    external_id=pr_number,
                    request_payload=artifacts.model_dump(),
                    response_payload={"number": pr_number, "head": head_branch},
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
                        "adapter": "github_pr",
                        "pr_number": pr_number,
                        "head": head_branch,
                        "summary": artifacts.summary,
                    },
                )
            )
        return Receipt(id=receipt_id, external_id=pr_number)

    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision:
        """Poll PR comments until an approve/reject keyword appears or we time out."""
        if not receipt.external_id:
            return Decision(
                approve=False, reject=True, comment="missing receipt external_id"
            )
        pr_number = receipt.external_id
        path = f"/repos/{self.repo}/pulls/{pr_number}/comments"

        async def _poll_until_decision() -> Decision:
            seen_ids: set[str] = set()
            while True:
                data = await self._request("GET", path)
                comments = data if isinstance(data, list) else []
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    cid = str(comment.get("id") or "")
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    body = comment.get("body") or ""
                    decision = self._classify_comment(body)
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
        """Add a comment to the run's PR, looked up via the external_receipts row."""
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            receipt = (
                await session.execute(
                    select(ExternalReceipt)
                    .where(ExternalReceipt.run_id == run_id)
                    .where(ExternalReceipt.adapter_type == "github_pr")
                    .where(ExternalReceipt.operation == "notify")
                    .order_by(ExternalReceipt.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if receipt is None or not receipt.external_id:
                raise ValueError(
                    f"no GitHub PR receipt found for run {run_id}; "
                    "cannot post comment"
                )
            pr_number = receipt.external_id

        await self._request(
            "POST",
            f"/repos/{self.repo}/pulls/{pr_number}/comments",
            json={"body": body},
        )

        async with session_scope() as session:
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="post_comment",
                    payload={
                        "adapter": "github_pr",
                        "pr_number": pr_number,
                        "body": body,
                    },
                )
            )