"""Spike: idempotency contract for external side-effect calls.

Task 11 of M1 (Spike). Adapted from the task brief — the brief imports
``orchestration.adapters.task_source.base`` and
``orchestration.adapters.task_source.csv``, which land in M2 and do not
exist yet. The contract under test is the *pattern*, not the concrete
adapter class, so we exercise it against a small in-test fake that
mimics what the M2 adapters will look like, hitting the real
``external_receipts`` table (migration ``001_initial_schema``) on the
Postgres test DB.

The contract being verified (see ``docs/spike-m1.md`` §"Idempotency
contract"):

1. Compute ``idempotency_key`` deterministically from
   ``(adapter_type, external_id, operation)``.
2. Before executing, look up an existing ``ExternalReceipt`` by key.
3. If found with status ``succeeded``, return cached ``response_payload``
   (no re-execution).
4. If not found, execute the side-effect and persist a row with status
   ``succeeded`` (or ``failed`` on error).
5. Independent keys are independent — a different ``external_id`` or
   ``operation`` produces a fresh receipt.

Notes on the test scaffolding:

* ``tests/conftest.py`` provides an autouse ``_isolate_state`` fixture
  that TRUNCATEs every table before each test, so each test starts on
  a fresh schema (and the existing rows from prior tests don't leak).
* ``session_scope()`` is the production helper from
  ``orchestration.storage.db``; we use it directly rather than going
  through a fixture so the test reads as the production code would.
* ``ExternalReceipt.run_id`` is ``ForeignKey("runs.id")``, so each
  test seeds a minimal ``Project`` → ``Task`` → ``Run`` chain to
  satisfy the FK. ``runs`` itself doesn't carry the idempotency key
  used here — that lives on ``external_receipts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import select

from orchestration.storage.db import session_scope
from orchestration.storage.models import ExternalReceipt, Project, Run, Task

# ---------------------------------------------------------------------------
# In-test fake adapter: stands in for the M2 ``TaskSourceAdapter`` / ``CsvAdapter``
# pair the brief refers to. The shape mirrors what those adapters will need
# (a method per external operation, each gated by a deterministic key + a
# ``ExternalReceipt`` lookup) without pulling in code that does not exist yet.
# ---------------------------------------------------------------------------


@dataclass
class FakeTaskSourceAdapter:
    """Minimal stand-in for the M2 ``TaskSourceAdapter`` interface.

    ``calls`` records every successful execution — a test asserts the
    counter to prove the side-effect was (or was not) run. The payload
    returned by ``create_external_task`` is what would normally come
    back from a Jira/Linear/GitHub API call; here it's just a dict
    derived from the external_id so the test can inspect it.
    """

    name: str
    calls: list[str] = field(default_factory=list)

    async def create_external_task(
        self,
        run_id: UUID,
        external_id: str,
        operation: str = "create",
    ) -> dict:
        """Execute (or skip) the external ``operation`` idempotently.

        Key shape: ``"{adapter_type}:{external_id}:{operation}"`` —
        matches the brief's ``"task:{adapter.name}:T1:create"`` form
        while keeping the contract explicit (project_id is encoded in
        the key namespace via ``run_id`` when wired up in M2 — for the
        spike, ``adapter_type`` + ``external_id`` + ``operation`` is
        enough to prove uniqueness).
        """
        key = f"{self.name}:{external_id}:{operation}"
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(ExternalReceipt).where(
                        ExternalReceipt.idempotency_key == key
                    )
                )
            ).scalar_one_or_none()

            if existing is not None and existing.status == "succeeded":
                # Re-use path: do NOT call the external side-effect, do
                # NOT insert a new row. Return the cached response with
                # a marker so the test can tell which path was taken.
                cached = dict(existing.response_payload or {})
                cached["reused"] = True
                return cached

            # Fresh path: execute the side-effect (recorded in
            # ``self.calls``) and persist the receipt in the same
            # transaction.
            self.calls.append(f"{external_id}:{operation}")
            payload = {
                "external_id": external_id,
                "operation": operation,
                "fake_external_ref": f"ext-ref-{external_id}",
            }
            if existing is None:
                session.add(
                    ExternalReceipt(
                        run_id=run_id,
                        idempotency_key=key,
                        adapter_type=self.name,
                        operation=operation,
                        external_id=external_id,
                        request_payload={"external_id": external_id},
                        response_payload=payload,
                        status="succeeded",
                    )
                )
            else:
                # Receipt existed but was e.g. ``failed``; overwrite.
                existing.response_payload = payload
                existing.status = "succeeded"
            return payload


# ---------------------------------------------------------------------------
# Test fixtures — kept inline (not conftest) so the test file is
# self-contained and the spike reader can see the full setup.
# ---------------------------------------------------------------------------


async def _seed_run() -> tuple[UUID, UUID]:
    """Insert a minimal Project + Task + Run so ExternalReceipt FK is satisfied.

    Returns ``(run_id, project_id)``. Each test calls this once; the
    autouse ``_isolate_state`` fixture (from ``tests/conftest.py``)
    truncates everything between tests, so the IDs never collide.

    Flushes the parent rows before adding the child so SQLAlchemy
    doesn't try to INSERT the Run before its Task/Project exist —
    the dependency-based sort_tables walker doesn't always get the
    order right when three FK-linked rows are added in a single
    session, and the FK constraint then rejects the child insert.
    """
    async with session_scope() as session:
        project = Project(
            id=uuid4(),
            name="spike-project",
            repo_url="https://example.com/spike.git",
            default_branch="main",
        )
        session.add(project)
        await session.flush()

        task = Task(
            id=uuid4(),
            project_id=project.id,
            external_id="spike-T1",
            source_type="csv",
            title="Spike task",
            description=None,
            status="pending",
        )
        session.add(task)
        await session.flush()

        run = Run(
            id=uuid4(),
            project_id=project.id,
            task_id=task.id,
            flow_uuid=uuid4(),
            status="running",
            current_stage="plan",
        )
        session.add(run)
        await session.flush()
        return run.id, project.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_call_executes_and_persists_receipt() -> None:
    """First call has no prior receipt → adapter executes, row inserted.

    Validates rules 2 (lookup) and 5 (persist on first call) of the
    contract: with no existing row, the adapter runs the side-effect
    once and writes an ``ExternalReceipt`` with status ``succeeded``
    and a populated ``response_payload``.
    """
    run_id, _ = await _seed_run()
    adapter = FakeTaskSourceAdapter(name="fake-csv")

    payload = await adapter.create_external_task(run_id=run_id, external_id="T1")

    assert adapter.calls == ["T1:create"], (
        f"side-effect should have run exactly once; got {adapter.calls!r}"
    )
    assert payload == {
        "external_id": "T1",
        "operation": "create",
        "fake_external_ref": "ext-ref-T1",
    }, f"first call should return the freshly produced payload; got {payload!r}"
    assert "reused" not in payload, (
        "first call must NOT mark the payload as reused — that flag is "
        f"only set on the dedup short-circuit path; payload={payload!r}"
    )

    # Receipt persisted.
    async with session_scope() as session:
        stored = (
            await session.execute(
                select(ExternalReceipt).where(
                    ExternalReceipt.idempotency_key == "fake-csv:T1:create"
                )
            )
        ).scalar_one()
        assert stored.status == "succeeded", (
            f"first-call receipt should be 'succeeded'; got {stored.status!r}"
        )
        assert stored.adapter_type == "fake-csv"
        assert stored.operation == "create"
        assert stored.external_id == "T1"
        assert stored.run_id == run_id
        assert stored.response_payload == {
            "external_id": "T1",
            "operation": "create",
            "fake_external_ref": "ext-ref-T1",
        }, (
            f"response_payload should mirror the returned payload; "
            f"got {stored.response_payload!r}"
        )


async def test_second_call_with_same_key_skips_execution() -> None:
    """Second call with the same idempotency_key short-circuits.

    Validates rule 3 of the contract: when an existing receipt with
    status ``succeeded`` is found, the adapter MUST NOT call the
    external side-effect, MUST NOT insert a new row, and MUST return
    the cached ``response_payload`` (annotated so callers can tell).
    """
    run_id, _ = await _seed_run()
    adapter = FakeTaskSourceAdapter(name="fake-csv")

    # First call seeds the receipt.
    first = await adapter.create_external_task(run_id=run_id, external_id="T1")
    assert adapter.calls == ["T1:create"]

    # Second call with the same key — must NOT execute.
    second = await adapter.create_external_task(run_id=run_id, external_id="T1")
    assert adapter.calls == ["T1:create"], (
        f"second call must skip the side-effect; calls went from "
        f"{['T1:create']!r} to {adapter.calls!r}"
    )
    assert second["reused"] is True, (
        f"second call must mark the payload as reused; got {second!r}"
    )
    assert second == first | {"reused": True}, (
        f"cached payload should match first call + reused flag; "
        f"first={first!r}, second={second!r}"
    )

    # Exactly one receipt row — no duplicate from the second call.
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ExternalReceipt).where(
                    ExternalReceipt.idempotency_key == "fake-csv:T1:create"
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            f"second call must NOT insert a duplicate receipt; "
            f"found {len(rows)} rows for key 'fake-csv:T1:create'"
        )


async def test_different_external_id_means_independent_execution() -> None:
    """A different ``external_id`` produces a different key → fresh run.

    Validates rule 1 (key must include ``external_id``) and the
    corollary that genuinely distinct operations do not dedup onto
    each other. Without this property a retry could swallow a
    different task's receipt.
    """
    run_id, _ = await _seed_run()
    adapter = FakeTaskSourceAdapter(name="fake-csv")

    a = await adapter.create_external_task(run_id=run_id, external_id="T1")
    b = await adapter.create_external_task(run_id=run_id, external_id="T2")

    assert adapter.calls == ["T1:create", "T2:create"], (
        f"distinct external_ids should each run the side-effect; "
        f"got {adapter.calls!r}"
    )
    assert "reused" not in a and "reused" not in b, (
        f"neither first call for an external_id should be marked reused; "
        f"a={a!r}, b={b!r}"
    )
    assert a["external_id"] == "T1" and b["external_id"] == "T2"

    # Two distinct receipts.
    async with session_scope() as session:
        keys = (
            await session.execute(
                select(ExternalReceipt.idempotency_key).where(
                    ExternalReceipt.run_id == run_id
                )
            )
        ).scalars().all()
        assert set(keys) == {"fake-csv:T1:create", "fake-csv:T2:create"}, (
            f"each external_id should produce its own receipt; got {keys!r}"
        )


async def test_failed_receipt_allows_retry() -> None:
    """A ``failed`` receipt does not block subsequent attempts.

    The contract (rule 3) short-circuits on ``status == 'succeeded'``
    only; a prior failed attempt must NOT block a fresh retry, or the
    system would get stuck after any transient error. This test
    verifies that the dedup check is keyed on ``status``, not just on
    row existence — important because rule 5 says we may overwrite a
    failed row on the next attempt.
    """
    run_id, _ = await _seed_run()
    adapter = FakeTaskSourceAdapter(name="fake-csv")

    # Seed a failed receipt directly to simulate a prior attempt that
    # crashed mid-side-effect (the spike doesn't have a failure-injection
    # path through the adapter — keeping it explicit is clearer).
    key = "fake-csv:T1:create"
    async with session_scope() as session:
        session.add(
            ExternalReceipt(
                run_id=run_id,
                idempotency_key=key,
                adapter_type="fake-csv",
                operation="create",
                external_id="T1",
                request_payload={"external_id": "T1"},
                response_payload=None,
                status="failed",
            )
        )

    payload = await adapter.create_external_task(run_id=run_id, external_id="T1")

    assert adapter.calls == ["T1:create"], (
        f"a prior failed receipt must not block a fresh attempt; "
        f"calls went from [] to {adapter.calls!r}"
    )
    assert "reused" not in payload, (
        f"retry of a failed receipt must NOT be marked reused; "
        f"payload={payload!r}"
    )

    # The failed row was overwritten to succeeded — still exactly one row.
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ExternalReceipt).where(
                    ExternalReceipt.idempotency_key == key
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            f"retry should overwrite the failed row, not duplicate it; "
            f"found {len(rows)} rows"
        )
        assert rows[0].status == "succeeded", (
            f"after successful retry the row should be 'succeeded'; "
            f"got {rows[0].status!r}"
        )
        assert rows[0].response_payload == payload
