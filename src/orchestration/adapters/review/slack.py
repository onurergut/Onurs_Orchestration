"""Slack Block Kit review surface adapter."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import insert, select

from orchestration.adapters.review.base import (
    Artifacts,
    Decision,
    Receipt,
    ReviewSurfaceAdapter,
)
from orchestration.storage.db import session_scope
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.redis import get_redis


class SlackAdapter(ReviewSurfaceAdapter):
    """Review surface backed by Slack interactive Block Kit messages."""

    name = "slack"

    def __init__(
        self,
        bot_token: str | None = None,
        channel: str | None = None,
        default_channel: str = "#orchestration",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.bot_token = (
            bot_token if bot_token is not None else os.environ.get("SLACK_BOT_TOKEN")
        )
        self.channel = (
            channel
            if channel is not None
            else os.environ.get("SLACK_CHANNEL", default_channel)
        )
        if not self.bot_token:
            raise ValueError("SlackAdapter requires env var SLACK_BOT_TOKEN")
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    @staticmethod
    def _url(path: str) -> str:
        return f"https://slack.com/api/{path.lstrip('/')}"

    async def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                self._url(path), headers=self._headers(), json=payload
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("ok", False):
                error = data.get("error", "unknown error") if isinstance(data, dict) else data
                raise RuntimeError(f"Slack API {path} failed: {error}")
            return data
        finally:
            if owns_client:
                await client.aclose()

    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt:
        channel = self.channel
        if not channel:
            raise ValueError("SlackAdapter requires a channel")
        payload = {
            "channel": channel,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Review needed"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": artifacts.summary},
                },
                {
                    "type": "actions",
                    "block_id": "review_actions",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "action_id": "approve",
                            "value": str(run_id),
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "action_id": "reject",
                            "value": str(run_id),
                        },
                    ],
                },
            ],
        }
        data = await self._request("chat.postMessage", payload)
        ts = data.get("ts")
        if not isinstance(ts, str) or not ts:
            raise RuntimeError("Slack API response did not contain message ts")

        external_id = f"{channel}:{ts}"
        async with session_scope() as session:
            await session.execute(
                insert(ExternalReceipt).values(
                    id=uuid4(),
                    run_id=run_id,
                    idempotency_key=f"slack:notify:{run_id}",
                    adapter_type=self.name,
                    operation="notify",
                    external_id=external_id,
                    request_payload=payload,
                    response_payload=data,
                    status="succeeded",
                )
            )
        # Slack message timestamps are decimal strings, while the shared Receipt
        # model currently annotates ``id`` as UUID. Keep the persisted receipt's
        # UUID for database integrity, but preserve Slack's timestamp on the
        # adapter receipt because it is the interactive-webhook correlation key.
        return Receipt.model_construct(id=ts, external_id=external_id)

    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision:
        redis = get_redis()
        channel = f"slack:decision:{receipt.id}"
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async with asyncio.timeout(timeout_s):
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        data = msg["data"]
                        if isinstance(data, bytes):
                            data = data.decode()
                        return Decision(**json.loads(data))
        except TimeoutError:
            return Decision(approve=False, reject=True, comment="timeout")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        raise RuntimeError("Slack decision listener stopped without a decision")

    async def post_comment(self, run_id: UUID, body: str) -> None:
        async with session_scope() as session:
            receipt = (
                await session.execute(
                    select(ExternalReceipt)
                    .where(ExternalReceipt.run_id == run_id)
                    .where(ExternalReceipt.adapter_type == self.name)
                    .where(ExternalReceipt.operation == "notify")
                    .order_by(ExternalReceipt.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if receipt is None or not receipt.external_id:
            raise ValueError(
                f"no Slack review receipt found for run {run_id}; call notify_review first"
            )
        try:
            channel, thread_ts = str(receipt.external_id).rsplit(":", 1)
        except ValueError as exc:
            raise ValueError(f"invalid Slack receipt external_id: {receipt.external_id}") from exc
        await self._request(
            "chat.postMessage",
            {"channel": channel, "thread_ts": thread_ts, "text": body},
        )
