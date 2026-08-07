"""Tests for TesterStage."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.stages.base import StageContext
from orchestration.stages.tester import TesterStage, TestReport

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_tester_stage_metadata():
    stage = TesterStage()
    assert stage.name == "tester"
    assert stage.output_schema is TestReport
    assert stage.output_schema.__name__ == "TestReport"


def test_tester_implements_stage_crew_protocol():
    stage = TesterStage()
    assert hasattr(stage, "name")
    assert hasattr(stage, "output_schema")
    assert hasattr(stage, "run")
    assert callable(stage.run)


def test_test_report_schema():
    report = TestReport(passed=1, failed=0, coverage_pct=100.0, flaky=[])
    assert report.passed == 1
    assert report.failed == 0
    assert report.coverage_pct == 100.0
    assert report.flaky == []


def test_parse_junit_missing_file(tmp_path: Path):
    passed, failed, flaky = TesterStage._parse_junit(tmp_path / "nope.xml")
    assert (passed, failed, flaky) == (0, 0, [])


def test_parse_coverage_missing_file(tmp_path: Path):
    assert TesterStage._parse_coverage(tmp_path / "nope.xml") == 0.0


def test_parse_coverage_multiplies_by_100(tmp_path: Path):
    cov = tmp_path / "coverage.xml"
    cov.write_text('<?xml version="1.0"?><coverage line-rate="0.85"></coverage>')
    assert TesterStage._parse_coverage(cov) == pytest.approx(85.0)


def test_parse_junit_extracts_passed_failed_flaky(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" errors="1" skipped="0">
    <testcase classname="tests.test_a" name="test_pass"/>
    <testcase classname="tests.test_a" name="test_fail">
      <failure message="boom">assertion failed</failure>
    </testcase>
    <testcase classname="tests.test_b" name="test_err">
      <error message="boom">exception</error>
    </testcase>
  </testsuite>
</testsuites>
"""
    )
    passed, failed, flaky = TesterStage._parse_junit(junit)
    assert passed == 1  # 3 - 1 failure - 1 error
    assert failed == 1
    assert flaky == [
        "tests.test_a.test_fail",
        "tests.test_b.test_err",
    ]


def test_parse_junit_handles_bare_testsuite_root(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" failures="0" errors="0">
  <testcase classname="m" name="t1"/>
  <testcase classname="m" name="t2"/>
</testsuite>
"""
    )
    passed, failed, flaky = TesterStage._parse_junit(junit)
    assert passed == 2
    assert failed == 0
    assert flaky == []


@pytest.mark.asyncio
async def test_tester_runs_against_tmp_worktree(tmp_path: Path):
    """Integration: run TesterStage on a tmp worktree with one passing test."""
    (tmp_path / "test_smoke.py").write_text(
        "def test_x():\n    assert True\n"
    )

    stage = TesterStage(pytest_executable=None)
    ctx = StageContext(
        run_id=uuid4(),
        project_id=uuid4(),
        task_external_id="22",
        task_title="smoke",
        task_description=None,
        worktree_path=tmp_path,
        previous_artifacts={},
    )

    report = await stage.run(ctx, llm=None)  # type: ignore[arg-type]

    assert isinstance(report, TestReport)
    assert report.passed == 1
    assert report.failed == 0
    assert report.flaky == []
    assert report.coverage_pct == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_tester_reports_failures_and_flaky(tmp_path: Path):
    """Integration: a failing test surfaces in `failed` and `flaky`."""
    (tmp_path / "test_smoke.py").write_text(
        "def test_x():\n    assert True\n"
        "\n"
        "def test_y():\n    assert False\n"
    )

    stage = TesterStage(pytest_executable=None)
    ctx = StageContext(
        run_id=uuid4(),
        project_id=uuid4(),
        task_external_id="22",
        task_title="smoke",
        task_description=None,
        worktree_path=tmp_path,
        previous_artifacts={},
    )

    report = await stage.run(ctx, llm=None)  # type: ignore[arg-type]

    assert report.passed == 1
    assert report.failed == 1
    assert len(report.flaky) == 1
    assert report.flaky[0].endswith("test_y")