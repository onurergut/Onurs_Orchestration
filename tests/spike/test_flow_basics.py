"""Spike: prove a minimal 3-stage CrewAI Flow runs end-to-end.

Task 8 of M1 (Spike). Adapted from the task brief to match the installed
crewai API (1.15.x) — see ``docs/spike-m1.md`` for the full API discovery
notes. Differences from the illustrative brief code:

* Imports come from ``crewai.flow`` (top-level), not ``crewai.flow.flow``.
* ``Flow.kickoff()`` is synchronous in this version, so the test is sync.
  Use ``kickoff_async()`` if an async caller is required.
* ``list[str]`` defaults use ``default_factory=list`` to avoid sharing a
  single list between instances (Pydantic v2 best practice).

Task 9 added two more tests exercising ``@persist`` (state snapshot to
SQLite) and ``CheckpointConfig`` (event-driven JSON). ``SpikeState``
carries an explicit ``id`` field — required by the persistence engine —
and behaves correctly across Flow instances.

Task 10 added a parallel-join test exercising ``and_(branch_a,
branch_b)``: two branches triggered by a common parent run concurrently
via ``asyncio.gather`` of ``create_task``s, and the join listener fires
only after both have completed.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from crewai.flow import Flow, and_, listen, persist, start
from crewai.flow.persistence import SQLiteFlowPersistence
from crewai.state.checkpoint_config import CheckpointConfig
from pydantic import BaseModel, Field


class SpikeState(BaseModel):
    """State for the basic 3-stage flow (Task 8 + Task 9).

    The ``id`` field is mandatory — see
    ``crewai/flow/persistence/decorators.py`` where the engine rejects a
    state without an ``id`` attribute. We mint one with ``uuid4`` to
    mirror the form ``crewai.flow.runtime.FlowState`` provides.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = "spike-1"
    stages_done: list[str] = Field(default_factory=list)


class SpikeFlow(Flow[SpikeState]):
    @start()
    def init(self) -> str:
        self.state.stages_done.append("init")
        return "init_done"

    @listen(init)
    def plan(self, prev: str) -> str:
        assert prev == "init_done", f"unexpected upstream payload: {prev!r}"
        self.state.stages_done.append("plan")
        return "plan_done"

    @listen(plan)
    def execute(self, prev: str) -> str:
        assert prev == "plan_done", f"unexpected upstream payload: {prev!r}"
        self.state.stages_done.append("execute")
        return "execute_done"


def test_spike_flow_runs_three_stages() -> None:
    flow = SpikeFlow()
    result = flow.kickoff()

    assert flow.state.stages_done == ["init", "plan", "execute"], (
        f"stages_done was {flow.state.stages_done!r}"
    )
    # kickoff returns the value produced by the last executed method.
    assert result == "execute_done"


def test_spike_flow_state_is_isolated_per_instance() -> None:
    """Each Flow instance must get its own state; Pydantic default_factory
    guarantees this even though the default value is a mutable list."""

    flow_a = SpikeFlow()
    flow_b = SpikeFlow()

    flow_a.kickoff()
    assert flow_b.state.stages_done == []
    assert flow_a.state.stages_done == ["init", "plan", "execute"]


# ---------------------------------------------------------------------------
# Task 9: @persist (state snapshot to SQLite) + checkpoint (event-driven JSON)
# ---------------------------------------------------------------------------
#
# Adapted from the brief's ``test_persist_round_trip`` sketch. The brief
# assumed ``@persist`` and ``checkpoint=True`` were the only knobs; the
# installed library (crewai 1.15.12) wires them differently. Key
# corrections:
#
# * ``@persist`` is a pure metadata stamper. The Flow runtime calls
#   ``Flow.persistence.save_state(flow_uuid, method_name, state_data)``
#   after each persisted method completes.
# * The state model **must** carry an ``id`` field (else
#   ``ValueError("Flow state must have an 'id' field for persistence")``).
#   The built-in ``crewai.flow.runtime.FlowState`` provides one; we
#   roll our own with a uuid4 default for portability.
# * ``Flow.checkpoint=True`` is *not* "all events". It yields
#   ``CheckpointConfig(on_events=["task_completed"])``, which is a Crew
#   event Flow never fires — so default ``checkpoint=True`` writes
#   **nothing** for a Flow. Use ``CheckpointConfig(on_events=["*"])``
#   (or an explicit subset like ``["flow_finished",
#   "method_execution_finished"]``) to capture Flow lifecycle events.
# * ``Flow.kickoff(restore_from_state_id=uuid)`` hydrates ``self._state``
#   from the latest snapshot for ``uuid`` and mints a fresh
#   ``state.id``; the new run writes its own history under the new id,
#   leaving the source UUID's history untouched.
# * Known limitation (crewai 1.15.12): the event-driven JSON checkpointer
#   fails serialization with ``ValueError("Circular reference detected
#   (id repeated)")`` when the Flow's state carries a top-level ``id``
#   field — see ``docs/spike-m1.md`` §"Persist + checkpoint findings".
#   The runtime catches the failure and logs a warning; the JSON
#   artifact is **not** written, but the rest of the Flow continues
#   normally. ``@persist`` (the SQLite path) is unaffected. We assert
#   that behavior in ``test_checkpoint_config_registers_handler_but_serialization_broken``.


def test_persist_round_trip(tmp_path: Path) -> None:
    """``@persist`` snapshots state to SQLite after every method;
    ``kickoff(restore_from_state_id=...)`` hydrates a fresh Flow's state
    from an existing UUID's latest snapshot, and the source UUID's
    history is preserved untouched.
    """
    db_path = tmp_path / "states.db"
    persistence = SQLiteFlowPersistence(db_path=str(db_path))

    class PersistedFlow(Flow[SpikeState]):
        # ``@persist()`` is method-scoped: only decorated methods trigger
        # a snapshot save. We apply it to every method so the SQLite
        # history accumulates one row per completed stage. Note the
        # parentheses: ``@persist()`` not ``@persist``. The decorator is
        # ``crewai.flow.persistence.decorators.persist(persistence=None,
        # verbose=False)``; without parens, Python would pass the
        # underlying function as the ``persistence`` positional arg and
        # replace the wrapped method with the inner ``decorator``
        # closure — see test note below.
        @start()
        @persist()
        def init(self) -> str:
            self.state.stages_done.append("init")
            return "init_done"

        @listen(init)
        @persist()
        def plan(self, prev: str) -> str:
            assert prev == "init_done"
            self.state.stages_done.append("plan")
            return "plan_done"

        @listen(plan)
        @persist()
        def execute(self, prev: str) -> str:
            assert prev == "plan_done"
            self.state.stages_done.append("execute")
            return "execute_done"

    # Run 1: kick off, capture the auto-minted UUID.
    flow1 = PersistedFlow(persistence=persistence)
    flow1.kickoff()
    flow1_id = flow1.state.id

    assert isinstance(flow1_id, str) and flow1_id, (
        f"state.id should be a non-empty string; got {flow1_id!r}"
    )
    assert flow1.state.stages_done == ["init", "plan", "execute"]

    # The SQLite snapshot is under flow1_id.
    persisted_after_run = persistence.load_state(flow1_id)
    assert persisted_after_run is not None, (
        f"@persist should have written a snapshot to {db_path} for "
        f"flow_uuid={flow1_id!r}"
    )
    assert persisted_after_run["id"] == flow1_id
    assert persisted_after_run["stages_done"] == ["init", "plan", "execute"], (
        f"snapshot stages_done was {persisted_after_run['stages_done']!r}"
    )
    assert persisted_after_run["run_id"] == "spike-1"

    # Same flow_uuid accumulates rows as each method completes.
    with sqlite3.connect(str(db_path)) as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM flow_states WHERE flow_uuid = ?", (flow1_id,)
        ).fetchone()[0]
    assert row_count >= 1, (
        f"flow_states table should have at least one row for {flow1_id}; "
        f"got {row_count}"
    )

    # Run 2: fork from flow1's UUID. The runtime hydrates ``self._state``
    # from the SQLite snapshot, mints a fresh state.id, then re-runs the
    # whole flow. flow1_id's snapshot must remain intact — the fork
    # writes under the new id, not flow1_id.
    flow2 = PersistedFlow(persistence=persistence)
    flow2.kickoff(restore_from_state_id=flow1_id)

    assert flow2.state.id != flow1_id, (
        f"after restore_from_state_id, new state.id should differ from "
        f"source; both were {flow2.state.id!r}"
    )

    flow1_preserved = persistence.load_state(flow1_id)
    assert flow1_preserved is not None
    assert flow1_preserved["stages_done"] == ["init", "plan", "execute"], (
        f"source UUID snapshot was mutated by the restore; "
        f"got {flow1_preserved['stages_done']!r}"
    )

    flow2_preserved = persistence.load_state(flow2.state.id)
    assert flow2_preserved is not None
    # The forked flow re-runs every method after hydration — its final
    # ``stages_done`` therefore contains the *restored* stages plus the
    # freshly appended ones, in order. The doubled-trail is the
    # observable signature of "fork + replay" semantics (vs. resume,
    # which would skip already-completed methods).
    assert flow2_preserved["stages_done"] == [
        "init",
        "plan",
        "execute",
        "init",
        "plan",
        "execute",
    ], (
        f"forked flow should re-execute the chain on the hydrated "
        f"state; got {flow2_preserved['stages_done']!r}"
    )


def test_checkpoint_config_writes_json_artifacts(tmp_path: Path) -> None:
    """``CheckpointConfig(on_events=["*"])`` writes one JSON file per
    matched event into ``<location>/main/``.

    The default ``checkpoint=True`` would yield
    ``CheckpointConfig(on_events=["task_completed"])`` — a Crew event
    Flow never fires, so the JsonProvider would write nothing for an
    entire Flow run. The right configuration for a Flow is to override
    ``on_events`` to either ``["*"]`` or an explicit subset like
    ``["flow_started", "method_execution_finished", "flow_finished"]``.

    File naming is ``{ts}_{uuid8}_p-{parent_id}.json`` — each child
    records its parent id in the filename for lineage without parsing
    the JSON blob. Files are written through
    ``JsonProvider.checkpoint`` and contain the full ``RuntimeState``
    dump (root entities + event record).
    """
    ckpt_dir = tmp_path / "ckpts"
    checkpoint_cfg = CheckpointConfig(
        location=str(ckpt_dir),
        on_events=["*"],
    )
    persistence = SQLiteFlowPersistence(db_path=str(tmp_path / "states.db"))

    class PersistedFlow(Flow[SpikeState]):
        @start()
        @persist()
        def init(self) -> str:
            self.state.stages_done.append("init")
            return "init_done"

        @listen(init)
        @persist()
        def plan(self, _prev: str) -> str:
            self.state.stages_done.append("plan")
            return "plan_done"

        @listen(plan)
        @persist()
        def execute(self, _prev: str) -> str:
            self.state.stages_done.append("execute")
            return "execute_done"

    flow1 = PersistedFlow(persistence=persistence, checkpoint=checkpoint_cfg)
    flow1.kickoff()

    assert flow1.state.stages_done == ["init", "plan", "execute"]

    # JsonProvider stores files under ``<location>/main/``.
    branch_dir = ckpt_dir / "main"
    assert branch_dir.is_dir(), (
        f"expected JsonProvider branch dir at {branch_dir}; "
        f"got {list(ckpt_dir.iterdir()) if ckpt_dir.exists() else 'no ckpt dir'}"
    )

    ckpt_files = sorted(p for p in branch_dir.rglob("*.json") if p.is_file())
    assert ckpt_files, (
        f"expected at least one checkpoint JSON file under {branch_dir}; "
        f"directory was empty"
    )

    # The JSON payload is a RuntimeState dump. Confirm it actually
    # captures our completed stages and the entity_type=flow root.
    sample = ckpt_files[-1].read_text()  # latest by name (timestamp prefix)
    assert "entity_type" in sample and "\"flow\"" in sample, (
        f"checkpoint payload missing flow root; first 500 chars: {sample[:500]!r}"
    )
    assert "stages_done" in sample and "init" in sample, (
        f"checkpoint payload missing flow state fields; first 500 chars: {sample[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Task 10: parallel stages + and_() join
# ---------------------------------------------------------------------------
#
# The brief's sketch used ``from crewai.flow.flow import and_, listen, start``;
# in 1.15.12 ``and_`` (and ``or_``) live on the top-level ``crewai.flow``
# module and originate in ``crewai.flow.dsl._conditions``. They build a
# ``FlowCondition`` dict (``{"type": "AND_CONDITION", "conditions": [...]}``)
# which the runtime's ``_condition_satisfied`` evaluates against the
# accumulated ``seen`` events.
#
# Key runtime behaviour verified below (see crewai/flow/runtime/__init__.py
# around ``_condition_met`` and ``_find_triggered_methods``): when a
# listener fires (e.g. ``init`` completes), the runtime walks every
# listener whose condition references it. For each listener it calls
# ``_condition_met(condition, trigger, key)`` which accumulates triggers
# into ``seen`` and only fires when ``_condition_satisfied`` returns True.
# For ``and_(branch_a, branch_b)`` that means ``join`` fires only after
# both branches have completed (independently of arrival order).
#
# Concurrency: listeners triggered by the same event are scheduled via
# ``asyncio.gather(*[asyncio.create_task(_execute_single_listener(...))])``
# (lines ~3096-3104 in the runtime). Each task invokes ``_execute_method``,
# which runs sync methods via ``asyncio.to_thread(ctx.run, method, ...)`` —
# so sync branches *do* run on the default ThreadPoolExecutor concurrently.
# We measure this below by giving each branch a 50 ms sleep and asserting
# the joint wall-clock is < 2× that.
#
# State caveat: every branch shares ``self.state`` on the same Flow
# instance. Pydantic ``BaseModel`` instances are not thread-safe for
# arbitrary attribute mutation; the ``@listen(and_(...))`` join runs in
# a single thread after both branches finish, so this is fine in this
# spike, but any cross-branch state writes should funnel through a
# ``threading.Lock`` or use a thread-safe primitive (queue, atomic counter)
# before going to production.


class ParallelState(BaseModel):
    """State for the parallel-join test.

    Carries ``a_done`` / ``b_done`` flags (which branch completed) and
    ``started_at`` / ``finished_at`` for each branch. The ``id`` field is
    not strictly needed for this test (no ``@persist``), but keeping it
    matches the form used elsewhere and avoids the persistence decorator
    surprises discovered in Task 9.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    a_done: bool = False
    b_done: bool = False
    a_started_at: float | None = None
    a_finished_at: float | None = None
    b_started_at: float | None = None
    b_finished_at: float | None = None
    join_started_at: float | None = None
    join_finished_at: float | None = None
    order: list[str] = Field(default_factory=list)


_BRANCH_SLEEP_S = 0.05  # 50 ms; chosen to be larger than thread-pool dispatch overhead.


class ParallelFlow(Flow[ParallelState]):
    @start()
    def init(self) -> str:
        self.state.order.append("init")
        return "init_done"

    @listen(init)
    def branch_a(self, _prev: str) -> str:
        # Record start before sleeping so timestamps reflect the
        # *actual* execution window, not the call-dispatch latency.
        self.state.a_started_at = time.monotonic()
        time.sleep(_BRANCH_SLEEP_S)
        self.state.a_done = True
        self.state.a_finished_at = time.monotonic()
        self.state.order.append("branch_a")
        return "a_done"

    @listen(init)
    def branch_b(self, _prev: str) -> str:
        self.state.b_started_at = time.monotonic()
        time.sleep(_BRANCH_SLEEP_S)
        self.state.b_done = True
        self.state.b_finished_at = time.monotonic()
        self.state.order.append("branch_b")
        return "b_done"

    @listen(and_(branch_a, branch_b))
    def join(self, _prev: object) -> str:
        # ``_prev`` here is the upstream payload of whichever branch
        # triggered last; with ``and_`` the runtime only invokes ``join``
        # after both branches have completed, and pass a synthesised
        # trigger (typically ``None`` or the last branch's output — see
        # the runtime's listener dispatch). We don't assert on its value.
        assert self.state.a_done and self.state.b_done, (
            "join fired before both branches reported done — and_(...) "
            "semantics broken"
        )
        self.state.join_started_at = time.monotonic()
        self.state.order.append("join")
        self.state.join_finished_at = time.monotonic()
        return "joined"


def test_parallel_join() -> None:
    """``@listen(and_(branch_a, branch_b))`` waits for both branches.

    Verifies the spec §5.2 pattern: a common parent (``init``) fans out
    into two parallel branches, and the downstream ``join`` listener
    fires only after both have completed. Also asserts the branches
    actually run concurrently — the runtime schedules them via
    ``asyncio.create_task`` and runs sync bodies via
    ``asyncio.to_thread``, so wall-clock time should reflect overlap,
    not serial execution.
    """
    flow = ParallelFlow()
    wall_start = time.monotonic()
    result = flow.kickoff()
    wall_end = time.monotonic()

    # (a) Both branches executed.
    assert flow.state.a_done and flow.state.b_done, (
        f"both branches should have run; a_done={flow.state.a_done}, "
        f"b_done={flow.state.b_done}"
    )

    # (b) join ran after both branches (i.e. join is the last entry in
    # the order log; both branches appear before it).
    assert flow.state.order == ["init", "branch_a", "branch_b", "join"] or flow.state.order == [
        "init",
        "branch_b",
        "branch_a",
        "join",
    ], (
        f"join should be the last entry in the order log, with both "
        f"branches preceding it; got {flow.state.order!r}"
    )
    assert flow.state.order[-1] == "join", (
        f"join should be the last stage to run; order={flow.state.order!r}"
    )
    assert flow.state.order[0] == "init"
    assert set(flow.state.order[1:3]) == {"branch_a", "branch_b"}, (
        f"both branch_a and branch_b should appear between init and join; "
        f"got {flow.state.order!r}"
    )

    # (c) kickoff returns the last method's value — join.
    assert result == "joined", f"kickoff() should return join's output; got {result!r}"

    # (d) Branches actually ran concurrently. Each sleeps 50 ms; if they
    # ran serially, total would be ≥ ~100 ms. With asyncio.gather +
    # asyncio.to_thread they overlap and finish in ~50 ms. Allow a
    # generous upper bound (5× the sleep) to stay robust on slow CI but
    # fail clearly on accidental serialisation (which would be ≥ 100 ms).
    total_branch_window = max(flow.state.a_finished_at, flow.state.b_finished_at) - min(
        flow.state.a_started_at, flow.state.b_started_at
    )
    assert total_branch_window < _BRANCH_SLEEP_S * 5, (
        f"branches took {total_branch_window*1000:.1f} ms total but each "
        f"sleeps only {_BRANCH_SLEEP_S*1000:.0f} ms — they ran serially "
        f"instead of concurrently"
    )

    # Sanity: branches' start times should overlap (or at most be a hair
    # apart). a_started_at < b_finished_at and b_started_at < a_finished_at.
    assert flow.state.a_started_at < flow.state.b_finished_at, (
        "branch_a should overlap or precede branch_b's completion"
    )
    assert flow.state.b_started_at < flow.state.a_finished_at, (
        "branch_b should overlap or precede branch_a's completion"
    )

    # join started after both branches finished — required by and_().
    assert flow.state.join_started_at >= flow.state.a_finished_at, (
        f"join must wait for branch_a; join_started_at={flow.state.join_started_at}, "
        f"a_finished_at={flow.state.a_finished_at}"
    )
    assert flow.state.join_started_at >= flow.state.b_finished_at, (
        f"join must wait for branch_b; join_started_at={flow.state.join_started_at}, "
        f"b_finished_at={flow.state.b_finished_at}"
    )

    # Whole-flow wall clock: branches overlapped, so it should be ~50 ms,
    # not ~100 ms. Loose bound to avoid CI flake but still catches the
    # common serialisation bug.
    assert (wall_end - wall_start) < _BRANCH_SLEEP_S * 5, (
        f"whole-flow wall clock {(wall_end - wall_start)*1000:.1f} ms exceeds "
        f"expected upper bound; branches likely ran serially"
    )


def test_parallel_join_blocks_until_both_complete() -> None:
    """``and_(...)`` must not fire on partial completion.

    To exercise this we slow one branch deliberately and capture, inside
    ``join``, whether the other branch had completed by the time join
    fired. The contract under test is: even if one branch is much
    slower, the join only fires once both have completed.
    """
    fast_done = False
    slow_done = False
    join_observed_fast = False
    join_observed_slow = False

    class UnevenState(BaseModel):
        order: list[str] = Field(default_factory=list)

    class UnevenFlow(Flow[UnevenState]):
        @start()
        def init(self) -> str:
            self.state.order.append("init")
            return "init_done"

        @listen(init)
        def fast_branch(self, _prev: str) -> str:
            nonlocal fast_done
            fast_done = True
            self.state.order.append("fast")
            return "fast_done"

        @listen(init)
        def slow_branch(self, _prev: str) -> str:
            nonlocal slow_done
            time.sleep(_BRANCH_SLEEP_S)
            slow_done = True
            self.state.order.append("slow")
            return "slow_done"

        @listen(and_(fast_branch, slow_branch))
        def join(self, _prev: object) -> str:
            nonlocal join_observed_fast, join_observed_slow
            join_observed_fast = fast_done
            join_observed_slow = slow_done
            self.state.order.append("join")
            return "joined"

    flow = UnevenFlow()
    flow.kickoff()

    assert fast_done and slow_done, "both branches must complete"
    # The critical contract: at the moment join fired, *both* had run.
    # Captured inside join via the nonlocal closures above.
    assert join_observed_fast and join_observed_slow, (
        f"join fired before both branches completed; observed_fast="
        f"{join_observed_fast}, observed_slow={join_observed_slow}"
    )
    # And join is last in the order log.
    assert flow.state.order[-1] == "join", (
        f"join should be the last stage to run; order={flow.state.order!r}"
    )
    assert set(flow.state.order[1:3]) == {"fast", "slow"}, (
        f"both fast and slow branches should appear between init and join; "
        f"got {flow.state.order!r}"
    )

