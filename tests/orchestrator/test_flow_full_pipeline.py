"""Smoke test for the M3 full-pipeline OrchestratorFlow wiring.

The flow now has 7 stages: load_context -> planner -> coder -> (tester
|| security) -> reviewer -> docs. Live execution is intentionally out of
scope here: the M3 stages need a real LLM, bandit, git, and a worktree,
which is an integration test (covered later, not in M3/Task 26).

What we assert here:

1. The four new methods (tester_stage, security_stage, reviewer_stage,
   docs_stage) exist on the flow instance as FlowMethods, mirroring the
   pre-existing planner_stage / coder_stage / load_context.
2. Their trigger conditions match the M3 spec:
   - tester_stage and security_stage share ``and_(coder_stage)`` so the
     runtime schedules them as sibling branches off coder.
   - reviewer_stage waits on ``and_(tester_stage, security_stage)``
     (waits for both parallel branches).
   - docs_stage chains off reviewer_stage.
3. The flow honours ``initial_state`` so a state passed at construction
   time is what ``flow.state`` returns (the run_id round-trips).
4. ``OrchestratorState.completed_stages`` defaults to an empty list and
   two independent instances do not share that list (defensive — the
   pydantic ``BaseModel`` ``[]`` default is the right shape but we pin
   it so a future refactor doesn't silently regress to a shared
   mutable default).
"""

from __future__ import annotations

import uuid

from crewai.flow.flow_wrappers import ListenMethod, StartMethod

from orchestration.orchestrator.flow import OrchestratorFlow
from orchestration.orchestrator.state import OrchestratorState


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
    )


def test_flow_exposes_all_seven_stage_methods() -> None:
    """All four M3 stages plus the pre-existing three must be wired as
    FlowMethods on the constructed flow instance.
    """
    flow = OrchestratorFlow(initial_state=_make_state())

    # Pre-existing methods still wired.
    assert isinstance(flow.load_context, StartMethod)
    assert isinstance(flow.planner_stage, ListenMethod)
    assert isinstance(flow.coder_stage, ListenMethod)

    # New M3 methods.
    assert isinstance(flow.tester_stage, ListenMethod)
    assert isinstance(flow.security_stage, ListenMethod)
    assert isinstance(flow.reviewer_stage, ListenMethod)
    assert isinstance(flow.docs_stage, ListenMethod)


def test_flow_listener_wiring_matches_m3_spec() -> None:
    """The runtime-level listener wiring must reflect the M3 spec.

    We pull the parsed ``listen`` triggers out of
    ``flow._listener_methods()`` and assert each stage's upstream
    condition. Sequential stages carry a plain string trigger; the
    parallel fan-out and join carry ``{"and": [...]}`` dicts.

    ``_listener_methods()`` returns a list of ``(name, definition,
    condition)`` tuples — the ``definition`` carries the parsed
    ``listen`` payload. We index ``[1]`` (the FlowMethodDefinition)
    rather than ``[0]`` (the method name) so we read the trigger
    shape that the runtime will actually evaluate against.
    """
    flow = OrchestratorFlow(initial_state=_make_state())

    definitions = {
        name: definition
        for name, definition, _condition in flow._listener_methods()
    }

    assert definitions["planner_stage"].listen == "load_context"
    assert definitions["coder_stage"].listen == "planner_stage"

    # Parallel fan-out: tester and security both wait on coder_stage via
    # the same ``and_(coder_stage)`` condition. They share the trigger so
    # the runtime schedules them as siblings.
    assert definitions["tester_stage"].listen == {"and": ["coder_stage"]}
    assert definitions["security_stage"].listen == {"and": ["coder_stage"]}

    # Join: reviewer waits on both parallel branches.
    assert definitions["reviewer_stage"].listen == {
        "and": ["tester_stage", "security_stage"]
    }

    # Sequential tail.
    assert definitions["docs_stage"].listen == "reviewer_stage"


def test_flow_initial_state_is_honoured() -> None:
    """``initial_state=...`` round-trips through the Flow's state.

    The CrewAI runtime wraps the supplied state in a ``StateWithId``
    helper (adding an ``id`` field required by the persistence engine),
    so the wrapper is *not* the same object as the supplied
    ``OrchestratorState``. The resume story only needs the *fields* to
    round-trip — the Flow must read ``flow.state.run_id`` and find the
    value the worker passed in. We assert that plus a couple of the
    other fields as a smoke check.
    """
    state = _make_state()
    flow = OrchestratorFlow(initial_state=state)

    assert flow.state.run_id == state.run_id
    assert flow.state.project_id == state.project_id
    assert flow.state.task_id == state.task_id
    # The wrapper is a different model but the wrapped fields match.
    assert flow.state.completed_stages == state.completed_stages == []


def test_orchestrator_state_completed_stages_default_isolated() -> None:
    """Each OrchestratorState instance starts with its own empty
    completed_stages list. Pydantic v2 deep-copies bare ``[]`` defaults,
    but we pin the contract so a future regression (e.g. switching to
    ``[]`` without ``default_factory`` semantics, or to a shared mutable)
    is caught here rather than at the resume hydration site.
    """
    a = _make_state()
    b = _make_state()

    assert a.completed_stages == []
    assert b.completed_stages == []
    assert a.completed_stages is not b.completed_stages

    # Mutating one must not affect the other.
    a.completed_stages.append("planner")
    assert b.completed_stages == []