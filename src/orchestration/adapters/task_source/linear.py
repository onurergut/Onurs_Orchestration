"""Linear task source adapter.

Talks to Linear via its public GraphQL API. Auth is a personal API key
(``lin_api_...``) passed verbatim as the ``Authorization`` header — Linear
does not require a scheme prefix.

A Linear GraphQL response is JSON shaped as ``{"data": ..., "errors": [...]}``.
HTTP 200 with a non-empty ``errors`` array is still a failure, so
``_request`` inspects that field before returning ``data``.

The adapter maps a small internal status vocabulary (``todo``,
``in_progress``, ``done``, ``canceled``) to Linear's default workflow state
names. Teams with renamed or custom states can pass the Linear state UUID
directly to :meth:`update_status` to bypass the name lookup.
"""

from __future__ import annotations

import os
import re
from typing import Any
from uuid import UUID

import httpx

from orchestration.adapters.task_source.base import (
    Comment,
    CreateTask,
    ExternalTask,
    TaskSourceAdapter,
)

# Internal status vocabulary -> Linear default workflow state name.
# These match Linear's out-of-the-box workflow. Teams that have customised
# their workflow should pass the state UUID directly to ``update_status``.
INTERNAL_TO_LINEAR_STATE: dict[str, str] = {
    "todo": "Todo",
    "in_progress": "In Progress",
    "done": "Done",
    "canceled": "Canceled",
}

_LINEAR_TO_INTERNAL_STATE: dict[str, str] = {
    name.lower(): internal for internal, name in INTERNAL_TO_LINEAR_STATE.items()
}


# Linear state IDs are UUIDs. We detect them by shape so ``update_status``
# can accept either a UUID passthrough or an internal/Linear name.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


class LinearAdapter(TaskSourceAdapter):
    """Task source backed by a Linear team."""

    name = "linear"

    GRAPHQL_URL = "https://api.linear.app/graphql"

    def __init__(
        self,
        api_key: str | None = None,
        team_id: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.environ.get("LINEAR_API_KEY")
        )
        self.team_id = (
            team_id if team_id is not None else os.environ.get("LINEAR_TEAM_ID")
        )

        missing: list[str] = []
        if not self.api_key:
            missing.append("LINEAR_API_KEY")
        if not self.team_id:
            missing.append("LINEAR_TEAM_ID")
        if missing:
            raise ValueError(
                f"LinearAdapter requires env var(s): {', '.join(missing)}"
            )

        self._client = client
        self._timeout = timeout
        # Cache of {lowercased_state_name: state_id}, populated lazily by
        # :meth:`_load_state_cache` on the first name-based status update.
        self._state_cache: dict[str, str] | None = None

    # ------------------------------------------------------------ headers

    def _headers(self) -> dict[str, str]:
        # Linear accepts the raw API key as the Authorization value.
        # __init__ asserts both api_key and team_id are non-empty, so we
        # narrow the type here for mypy.
        assert self.api_key is not None
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "orchestration-linear-adapter",
        }

    # ------------------------------------------------------------ _request

    async def _request(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST a GraphQL query to Linear and return the ``data`` field.

        Raises ``RuntimeError`` if HTTP fails or the GraphQL response carries
        an ``errors`` array (Linear returns HTTP 200 with errors set when the
        query is malformed or hits an auth/permission problem).
        """
        headers = self._headers()
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                self.GRAPHQL_URL,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("Linear returned empty response body")
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError(f"Unexpected Linear response shape: {data!r}")
            errors = data.get("errors")
            if errors:
                first = errors[0] if isinstance(errors, list) and errors else {}
                msg = first.get("message") if isinstance(first, dict) else str(first)
                raise RuntimeError(f"Linear GraphQL error: {msg}")
            return data.get("data") or {}
        finally:
            if owns_client:
                await client.aclose()

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _to_external_task(issue: dict[str, Any]) -> ExternalTask:
        """Translate a Linear issue JSON object into an ExternalTask.

        Prefers the human-readable ``identifier`` (e.g. ``ENG-123``) for
        ``external_id`` over the UUID — that's what users paste into Linear
        and what shows up in commit messages.
        """
        state = issue.get("state") or {}
        state_name = state.get("name") if isinstance(state, dict) else None
        metadata: dict[str, Any] = {
            "id": issue.get("id"),
            "state": state_name,
            "internal_status": (
                LinearAdapter._linear_state_name_to_internal(state_name)
                if isinstance(state_name, str)
                else None
            ),
        }
        return ExternalTask(
            external_id=str(issue.get("identifier") or ""),
            title=issue.get("title") or "",
            description=issue.get("description"),
            metadata=metadata,
        )

    @staticmethod
    def _internal_to_linear_state_name(status: str) -> str | None:
        return INTERNAL_TO_LINEAR_STATE.get(status.strip().lower())

    @staticmethod
    def _linear_state_name_to_internal(name: str) -> str | None:
        return _LINEAR_TO_INTERNAL_STATE.get(name.strip().lower())

    async def _load_state_cache(self) -> None:
        """Fetch every workflow state for the configured team (cached)."""
        query = (
            "query WorkflowStates($teamId: String!) {"
            " workflowStates(filter: { team: { id: { eq: $teamId } } }) {"
            " nodes { id name } } }"
        )
        data = await self._request(query, {"teamId": self.team_id})
        nodes = (((data or {}).get("workflowStates") or {}).get("nodes") or [])
        cache: dict[str, str] = {}
        for n in nodes:
            if not isinstance(n, dict):
                continue
            name = n.get("name")
            sid = n.get("id")
            if isinstance(name, str) and isinstance(sid, str):
                cache[name.lower()] = sid
        self._state_cache = cache

    async def _resolve_state_id(self, state_name: str) -> str:
        """Resolve a Linear workflow-state name to its UUID.

        Loads and caches the team's workflow states on first call.
        """
        if self._state_cache is None:
            await self._load_state_cache()
        cache = self._state_cache or {}
        key = state_name.strip().lower()
        sid = cache.get(key)
        if not sid:
            available = ", ".join(sorted(cache.keys())) or "<none>"
            raise ValueError(
                f"no Linear workflow state matched {state_name!r}; "
                f"available: {available}"
            )
        return sid

    # ------------------------------------------------------------ ABC methods

    async def list_tasks(
        self, project_id: UUID, status: str | None
    ) -> list[ExternalTask]:
        # Always exclude completed/canceled; optionally narrow by state name.
        # Linear's IssueFilter exposes ``state.type.nin`` and ``state.name.eq``.
        if status:
            linear_name = (
                self._internal_to_linear_state_name(status) or status.strip()
            )
            state_filter: dict[str, Any] = {
                "name": {"eq": linear_name},
                "type": {"nin": ["completed", "canceled"]},
            }
        else:
            state_filter = {"type": {"nin": ["completed", "canceled"]}}

        query = (
            "query ListIssues($filter: IssueFilter!) {"
            " issues(filter: $filter) {"
            " nodes { id identifier title description state { name } } } }"
        )
        variables: dict[str, Any] = {
            "filter": {
                "team": {"id": {"eq": self.team_id}},
                "state": state_filter,
            }
        }
        data = await self._request(query, variables)
        nodes = (((data or {}).get("issues") or {}).get("nodes") or [])
        return [self._to_external_task(n) for n in nodes if isinstance(n, dict)]

    async def get_task(self, task_id: str) -> ExternalTask:
        # Linear's ``issue`` query accepts either a UUID or an identifier.
        query = (
            "query GetIssue($id: String!) {"
            " issue(id: $id) {"
            " id identifier title description state { name } } }"
        )
        data = await self._request(query, {"id": task_id})
        issue = (data or {}).get("issue")
        if not isinstance(issue, dict):
            raise TypeError(f"Linear issue not found: {task_id}")
        return self._to_external_task(issue)

    async def create_task(
        self, project_id: UUID, payload: CreateTask
    ) -> ExternalTask:
        query = (
            "mutation CreateIssue($input: IssueCreateInput!) {"
            " issueCreate(input: $input) {"
            " success"
            " issue { id identifier title description state { name } } } }"
        )
        variables: dict[str, Any] = {
            "input": {
                "teamId": self.team_id,
                "title": payload.title,
            }
        }
        if payload.description:
            variables["input"]["description"] = payload.description
        data = await self._request(query, variables)
        issue = (((data or {}).get("issueCreate") or {}).get("issue"))
        if not isinstance(issue, dict):
            raise TypeError("Linear issueCreate returned no issue payload")
        return self._to_external_task(issue)

    async def update_status(self, task_id: str, status: str) -> None:
        """Move an issue to a new state.

        ``status`` may be:
          * a Linear state UUID (passthrough — fastest, no lookup query),
          * an internal vocabulary token (``todo``, ``in_progress``,
            ``done``, ``canceled``),
          * a Linear workflow state name (e.g. ``"In Progress"``).
        """
        normalized = status.strip()
        if _looks_like_uuid(normalized):
            state_id = normalized
        else:
            target_name = (
                self._internal_to_linear_state_name(normalized) or normalized
            )
            state_id = await self._resolve_state_id(target_name)

        query = (
            "mutation UpdateIssueState($id: String!, $input: IssueUpdateInput!) {"
            " issueUpdate(id: $id, input: $input) { success } }"
        )
        await self._request(
            query, {"id": task_id, "input": {"stateId": state_id}}
        )

    async def add_comment(self, task_id: str, body: str) -> Comment:
        query = (
            "mutation CreateComment($input: CommentCreateInput!) {"
            " commentCreate(input: $input) {"
            " success"
            " comment { id body } } }"
        )
        data = await self._request(
            query, {"input": {"issueId": task_id, "body": body}}
        )
        payload = (((data or {}).get("commentCreate") or {}).get("comment"))
        if not isinstance(payload, dict):
            return Comment(external_id="", body=body)
        return Comment(
            external_id=str(payload.get("id") or ""),
            body=str(payload.get("body") or body),
        )