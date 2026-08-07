
from orchestration.stages.coder import CoderStage
from orchestration.stages.planner import PlannerStage


def test_planner_output_schema():
    assert PlannerStage.output_schema.__name__ == "Plan"


def test_coder_output_schema():
    assert CoderStage.output_schema.__name__ == "CodeChange"


def test_stage_crews_implement_protocol():
    for cls in (PlannerStage, CoderStage):
        assert hasattr(cls, "name")
        assert hasattr(cls, "output_schema")
        assert hasattr(cls, "run")
