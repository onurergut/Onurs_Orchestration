import uuid

import pytest

from orchestration.orchestrator.flow import OrchestratorFlow
from orchestration.orchestrator.state import OrchestratorState


def test_flow_state_initializes():
    state = OrchestratorState(run_id=uuid.uuid4(), project_id=uuid.uuid4(), task_id=uuid.uuid4())
    assert state.completed_stages == []
    assert state.stage_artifacts == {}