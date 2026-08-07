from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest

from orchestration.adapters.review.base import Artifacts, Receipt
from orchestration.adapters.review.slack import SlackAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models import ExternalReceipt, Project, Run, Task

BOT_TOKEN = "xoxb-test-token"
CHANNEL = "C01234567"
MESSAGE_TS = "1723000000.123456"


async def _seed_run() -> UUID:
    project_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    async with session_scope() as session:
        session.add(
            Project(
                id=project_id,
                name="slack-adapter-test",
                repo_url="https://example.com/slack-adapter.git",
                default_branch="main",
            )
        )
        await session.flush()
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                external_id="slack-task",
                source_type="csv",
                title="Review Slack adapter",
                status="pending",
            )
        )
        await session.flush()
        session.add(
            Run(
                id=run_id,
                project_id=project_id,
                task_id=task_id,
                flow_uuid=uuid4(),
                status="running",
                current_stage="review",
            )
        )
        await session.flush()
    return run_id


def _client(responses: list[dict], requests: list[httpx.Request]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=responses.pop(0))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakePubSub:
    def __init__(self, queue: asyncio.Queue[dict]) -> None:
        self.queue = queue
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def close(self) -> None:
        self.closed = True

    async def listen(self):
        while True:
            yield await self.queue.get()


class _FakeRedis:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.instance = _FakePubSub(self.queue)

    def pubsub(self) -> _FakePubSub:
        return self.instance

    async def publish(self, channel: str, data: str) -> None:
        await self.queue.put({"type": "message", "channel": channel, "data": data})


@pytest.mark.asyncio
async def test_notify_review_posts_block_kit_and_returns_receipt():
    run_id = await _seed_run()
    requests: list[httpx.Request] = []
    client = _client([{"ok": True, "ts": MESSAGE_TS}], requests)
    adapter = SlackAdapter(bot_token=BOT_TOKEN, channel=CHANNEL, client=client)

    receipt = await adapter.notify_review(run_id, Artifacts(summary="Please review"))

    assert isinstance(receipt, Receipt)
    assert receipt.external_id == f"{CHANNEL}:{MESSAGE_TS}"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == f"Bearer {BOT_TOKEN}"
    payload = json.loads(request.content)
    assert payload == {
        "channel": CHANNEL,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Review needed"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Please review"},
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
    await client.aclose()


@pytest.mark.asyncio
async def test_await_decision_returns_published_approval(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "orchestration.adapters.review.slack.get_redis", lambda: fake_redis
    )
    adapter = SlackAdapter(bot_token=BOT_TOKEN, channel=CHANNEL)
    receipt = Receipt(id=uuid4(), external_id=f"{CHANNEL}:{MESSAGE_TS}")

    async def publish() -> None:
        await asyncio.sleep(0)
        await fake_redis.publish(
            f"slack:decision:{receipt.id}",
            json.dumps({"approve": True, "comment": "ship it"}),
        )

    publisher = asyncio.create_task(publish())
    decision = await adapter.await_decision(receipt, timeout_s=1)
    await publisher

    assert decision.approve is True
    assert decision.reject is False
    assert decision.comment == "ship it"
    assert fake_redis.instance.unsubscribed == [f"slack:decision:{receipt.id}"]
    assert fake_redis.instance.closed is True


@pytest.mark.asyncio
async def test_await_decision_timeout_rejects(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "orchestration.adapters.review.slack.get_redis", lambda: fake_redis
    )
    adapter = SlackAdapter(bot_token=BOT_TOKEN, channel=CHANNEL)
    receipt = Receipt(id=uuid4(), external_id=f"{CHANNEL}:{MESSAGE_TS}")

    decision = await adapter.await_decision(receipt, timeout_s=0.01)

    assert decision.approve is False
    assert decision.reject is True
    assert decision.comment == "timeout"
    assert fake_redis.instance.closed is True


@pytest.mark.asyncio
async def test_post_comment_replies_in_review_thread():
    run_id = await _seed_run()
    async with session_scope() as session:
        session.add(
            ExternalReceipt(
                id=uuid4(),
                run_id=run_id,
                idempotency_key=f"slack:notify:{run_id}",
                adapter_type="slack",
                operation="notify",
                external_id=f"{CHANNEL}:{MESSAGE_TS}",
                request_payload={"channel": CHANNEL},
                response_payload={"ok": True, "ts": MESSAGE_TS},
                status="succeeded",
            )
        )
        await session.flush()

    requests: list[httpx.Request] = []
    client = _client([{"ok": True, "ts": "1723000001.123456"}], requests)
    adapter = SlackAdapter(bot_token=BOT_TOKEN, channel=CHANNEL, client=client)

    result = await adapter.post_comment(run_id, "Follow-up details")

    assert result is None
    payload = json.loads(requests[0].content)
    assert payload == {
        "channel": CHANNEL,
        "thread_ts": MESSAGE_TS,
        "text": "Follow-up details",
    }
    await client.aclose()
