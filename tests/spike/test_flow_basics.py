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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from crewai.flow import Flow, listen, persist, start
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
