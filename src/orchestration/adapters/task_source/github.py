"""GitHub Issues task source adapter.

Reads from and writes to a GitHub repository's issues via the REST API v3.
Issues and PRs share the /issues endpoint; we filter to issues only by
excluding entries whose ``pull_request`` field is set.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx

from orchestration.adapters.task_source.base import (
    Comment,
    CreateTask,
    ExternalTask,
    TaskSourceAdapter,
)


class GitHubIssuesAdapter(TaskSourceAdapter):
    """Task source backed by a GitHub repository's issues."""

    name = "github"

    # GitHub REST API v3 base URL.
    API_BASE = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
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
                f"GitHubIssuesAdapter requires env var(s): {', '.join(missing)}"
            )

        self._client = client
        self._timeout = timeout

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
        # Merge any extra headers the caller passes (e.g., custom Content-Type).
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
    def _normalize_state(status: str | None) -> str:
        """Map a caller-supplied status to a GitHub issue ``state`` value."""
        if status is None:
            return "open"
        s = status.strip().lower()
        if s in {"closed", "close", "done", "resolved"}:
            return "closed"
        if s in {"all"}:
            return "all"
        return "open"

    @staticmethod
    def _is_issue(payload: dict[str, Any]) -> bool:
        """GitHub returns PRs on /issues; PRs carry a ``pull_request`` field."""
        return payload.get("pull_request") is None

    @staticmethod
    def _to_external_task(issue: dict[str, Any]) -> ExternalTask:
        """Translate a GitHub issue JSON object into an ExternalTask."""
        number = issue.get("number")
        labels = [label.get("name") for label in issue.get("labels", []) if label.get("name")]
        metadata: dict[str, Any] = {
            "url": issue.get("html_url"),
            "state": issue.get("state"),
            "labels": labels,
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "user": (issue.get("user") or {}).get("login"),
        }
        return ExternalTask(
            external_id=str(number) if number is not None else "",
            title=issue.get("title") or "",
            description=issue.get("body"),
            metadata=metadata,
        )

    # ------------------------------------------------------------- TaskSourceAdapter

    async def list_tasks(
        self, project_id: UUID, status: str | None
    ) -> list[ExternalTask]:
        state = self._normalize_state(status)
        params: dict[str, Any] = {"state": state, "per_page": 100}
        data = await self._request("GET", f"/repos/{self.repo}/issues", params=params)
        if not isinstance(data, list):
            return []
        return [self._to_external_task(item) for item in data if self._is_issue(item)]

    async def get_task(self, task_id: str) -> ExternalTask:
        data = await self._request("GET", f"/repos/{self.repo}/issues/{task_id}")
        if not isinstance(data, dict):
            raise TypeError(f"Unexpected response shape for issue {task_id}")
        return self._to_external_task(data)

    async def create_task(
        self, project_id: UUID, payload: CreateTask
    ) -> ExternalTask:
        body: dict[str, Any] = {"title": payload.title}
        if payload.description:
            body["body"] = payload.description
        # Surface caller-provided metadata as labels when possible; ignore unknown keys.
        extra = payload.metadata or {}
        if "labels" in extra and isinstance(extra["labels"], list):
            body["labels"] = [str(x) for x in extra["labels"]]
        data = await self._request(
            "POST", f"/repos/{self.repo}/issues", json=body
        )
        if not isinstance(data, dict):
            raise TypeError("Unexpected response shape when creating issue")
        return self._to_external_task(data)

    async def update_status(self, task_id: str, status: str) -> None:
        await self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/{task_id}",
            json={"state": self._normalize_state(status)},
        )

    async def add_comment(self, task_id: str, body: str) -> Comment:
        data = await self._request(
            "POST",
            f"/repos/{self.repo}/issues/{task_id}/comments",
            json={"body": body},
        )
        if not isinstance(data, dict):
            return Comment(external_id="", body=body)
        return Comment(
            external_id=str(data.get("id") or ""),
            body=str(data.get("body") or body),
        )
