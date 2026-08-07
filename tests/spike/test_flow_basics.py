"""Spike: prove a minimal 3-stage CrewAI Flow runs end-to-end.

Task 8 of M1 (Spike). Adapted from the task brief to match the installed
crewai API (1.15.x) — see ``docs/spike-m1.md`` for the full API discovery
notes. Differences from the illustrative brief code:

* Imports come from ``crewai.flow`` (top-level), not ``crewai.flow.flow``.
* ``Flow.kickoff()`` is synchronous in this version, so the test is sync.
  Use ``kickoff_async()`` if an async caller is required.
* ``list[str]`` defaults use ``default_factory=list`` to avoid sharing a
  single list between instances (Pydantic v2 best practice).
"""

from __future__ import annotations

from crewai.flow import Flow, listen, start
from pydantic import BaseModel, Field


class SpikeState(BaseModel):
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
