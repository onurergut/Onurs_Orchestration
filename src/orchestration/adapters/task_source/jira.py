"""Jira Cloud task source adapter.

Reads from and writes to a Jira Cloud project via the REST API v3
(``/rest/api/3/``). Auth is HTTP Basic over an API token (Atlassian
recommends an Atlassian account email + API token pair). Jira v3 requires
Atlassian Document Format (ADF) for ``description`` and ``comment.body``;
plain strings are rejected.
"""

from __future__ import annotations

import base64
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


class JiraAdapter(TaskSourceAdapter):
    """Task source backed by a Jira Cloud project."""

    name = "jira"

    def __init__(
        self,
        email: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
        project_key: str | None = None,
        done_transition_id: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
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
        self.done_transition_id = (
            done_transition_id
            if done_transition_id is not None
            else os.environ.get("JIRA_DONE_TRANSITION_ID")
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
                f"JiraAdapter requires env var(s): {', '.join(missing)}"
            )

        # Strip any trailing slash so URL composition is predictable.
        # Cast: mypy sees `self.base_url` as `str | None` per the attribute
        # type, but the `missing` check above already proved it's a non-empty str.
        self.base_url = self.base_url.rstrip("/")  # type: ignore[union-attr]

        self._client = client
        self._timeout = timeout

    # ------------------------------------------------------------------ helpers

    def _auth_header(self) -> str:
        raw = f"{self.email}:{self.token}".encode()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "orchestration-jira-adapter",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[Any]:
        """Issue an HTTP request against Jira and return parsed JSON."""
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

    # ---------------------------------------------------------- ADF conversions

    @staticmethod
    def _to_adf(text: str) -> dict[str, Any]:
        """Wrap a plain string as a minimal Atlassian Document Format doc."""
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }

    @staticmethod
    def _from_adf(adf: dict[str, Any]) -> str:
        """Recursively extract concatenated ``text`` fields from an ADF tree."""
        if not isinstance(adf, dict):
            return ""
        node_type = adf.get("type")
        text = adf.get("text")
        parts: list[str] = []
        if node_type == "text" and isinstance(text, str):
            parts.append(text)
        content = adf.get("content")
        if isinstance(content, list):
            for child in content:
                parts.append(JiraAdapter._from_adf(child))
        return "".join(parts)

    # ------------------------------------------------------------- translations

    @staticmethod
    def _to_external_task(issue: dict[str, Any]) -> ExternalTask:
        """Translate a Jira issue JSON object into an ExternalTask."""
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        description = fields.get("description")
        # `description` may be ADF, plain text (legacy), or null.
        if isinstance(description, dict):
            desc_text = JiraAdapter._from_adf(description)
        elif isinstance(description, str):
            desc_text = description
        else:
            desc_text = None

        status_obj = fields.get("status") or {}
        status_category = (status_obj.get("statusCategory") or {}).get("key")
        status_name = status_obj.get("name")
        priority = fields.get("priority") or {}
        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        labels = fields.get("labels") or []

        metadata: dict[str, Any] = {
            "id": issue.get("id"),
            "url": f"{issue.get('self', '').rsplit('/rest/', 1)[0] if issue.get('self') else ''}/browse/{key}",
            "status": status_name,
            "status_category": status_category,
            "priority": priority.get("name"),
            "assignee": assignee.get("displayName") or assignee.get("accountId"),
            "reporter": reporter.get("displayName") or reporter.get("accountId"),
            "labels": list(labels),
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "issue_type": (fields.get("issuetype") or {}).get("name"),
        }
        return ExternalTask(
            external_id=str(key),
            title=fields.get("summary") or "",
            description=desc_text,
            metadata=metadata,
        )

    @staticmethod
    def _extract_comment_body(raw: Any) -> str:
        """Pull plain text out of a Jira comment payload's ``body`` field."""
        if isinstance(raw, dict):
            return JiraAdapter._from_adf(raw)
        if isinstance(raw, str):
            return raw
        return ""

    # ------------------------------------------------------------- TaskSourceAdapter

    async def list_tasks(
        self, project_id: UUID, status: str | None
    ) -> list[ExternalTask]:
        # Always exclude Done via JQL; the optional ``status`` arg further
        # narrows by statusCategory (e.g., "todo", "in_progress") when given.
        jql_parts = [f"project={self.project_key}", "statusCategory != Done"]
        if status:
            jql_parts.append(f'statusCategory="{status}"')
        jql = " AND ".join(jql_parts)
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,description,status,priority,assignee,reporter,labels,created,updated,issuetype",
        }
        data = await self._request(
            "GET", "/rest/api/3/search", params=params
        )
        if not isinstance(data, dict):
            return []
        issues = data.get("issues") or []
        return [self._to_external_task(item) for item in issues if isinstance(item, dict)]

    async def get_task(self, task_id: str) -> ExternalTask:
        data = await self._request(
            "GET",
            f"/rest/api/3/issue/{task_id}",
            params={
                "fields": "summary,description,status,priority,assignee,reporter,labels,created,updated,issuetype"
            },
        )
        if not isinstance(data, dict):
            raise TypeError(f"Unexpected response shape for issue {task_id}")
        return self._to_external_task(data)

    async def create_task(
        self, project_id: UUID, payload: CreateTask
    ) -> ExternalTask:
        body: dict[str, Any] = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": payload.title,
                "description": self._to_adf(payload.description or ""),
            }
        }
        # Surface caller-provided metadata as labels when present.
        extra = payload.metadata or {}
        if isinstance(extra.get("labels"), list):
            body["fields"]["labels"] = [str(x) for x in extra["labels"]]
        data = await self._request("POST", "/rest/api/3/issue", json=body)
        if not isinstance(data, dict):
            raise TypeError("Unexpected response shape when creating issue")
        key = data.get("key")
        if not key:
            raise ValueError("Jira create response missing 'key'")
        # POST /issue returns the new key+id but no fields summary; fetch it.
        return await self.get_task(str(key))

    async def update_status(self, task_id: str, status: str) -> None:
        normalized = (status or "").strip().lower()
        # If caller asked for "done" and we have a transition id, use it.
        if normalized in {"done", "closed", "resolved"} and self.done_transition_id:
            await self._request(
                "POST",
                f"/rest/api/3/issue/{task_id}/transitions",
                json={"transition": {"id": str(self.done_transition_id)}},
            )
            return
        # Otherwise try a generic name match against the issue's transitions.
        # This keeps the adapter usable for any workflow without hard-coding
        # every transition ID, while still preferring the explicit override
        # for the common "done" case.
        transitions = await self._request(
            "GET", f"/rest/api/3/issue/{task_id}/transitions"
        )
        if not isinstance(transitions, dict):
            raise TypeError("Unexpected response shape for transitions")
        match_id: str | None = None
        for t in transitions.get("transitions") or []:
            if not isinstance(t, dict):
                continue
            name = (t.get("name") or "").strip().lower()
            if name == normalized:
                match_id = str(t.get("id"))
                break
        if not match_id:
            raise ValueError(
                f"no Jira transition matched status {status!r}; "
                "set JIRA_DONE_TRANSITION_ID or pass done_transition_id"
            )
        await self._request(
            "POST",
            f"/rest/api/3/issue/{task_id}/transitions",
            json={"transition": {"id": match_id}},
        )

    async def add_comment(self, task_id: str, body: str) -> Comment:
        data = await self._request(
            "POST",
            f"/rest/api/3/issue/{task_id}/comment",
            json={"body": self._to_adf(body)},
        )
        if not isinstance(data, dict):
            return Comment(external_id="", body=body)
        return Comment(
            external_id=str(data.get("id") or ""),
            body=self._extract_comment_body(data.get("body")) or body,
        )
