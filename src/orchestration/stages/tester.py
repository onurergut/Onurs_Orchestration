"""Tester stage: runs pytest in the worktree and parses JUnit XML + coverage XML.

Pure subprocess + XML parsing — no LLM call. Returns a `TestReport`.
"""

from __future__ import annotations

import asyncio
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew


class TestReport(BaseModel):
    passed: int
    failed: int
    coverage_pct: float
    flaky: list[str]

    # Tell pytest not to try to collect this BaseModel as a test class.
    __test__ = False


class TesterStage(StageCrew):
    name = "tester"
    output_schema = TestReport

    # Tell pytest not to try to collect this stage class as a test class.
    __test__ = False

    def __init__(self, pytest_executable: str = ".venv/bin/pytest"):
        self.pytest_executable = pytest_executable

    async def run(self, ctx: StageContext, llm: LLMClient) -> TestReport:
        cmd = [
            self.pytest_executable,
            "--junitxml=junit.xml",
            "--cov=.",
            "--cov-report=xml:coverage.xml",
            "-q",
        ]
        # Don't fail on non-zero exit: failing tests are data, not errors.
        # We still want to parse junit.xml/coverage.xml regardless of exit code.
        await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(ctx.worktree_path),
            capture_output=True,
            text=True,
        )
        return self._parse_reports(ctx.worktree_path)

    def _parse_reports(self, worktree_path: Path) -> TestReport:
        junit_path = worktree_path / "junit.xml"
        coverage_path = worktree_path / "coverage.xml"

        passed, failed, flaky = self._parse_junit(junit_path)
        coverage_pct = self._parse_coverage(coverage_path)

        return TestReport(
            passed=passed,
            failed=failed,
            coverage_pct=coverage_pct,
            flaky=flaky,
        )

    @staticmethod
    def _parse_junit(junit_path: Path) -> tuple[int, int, list[str]]:
        """Parse pytest's JUnit XML.

        Returns (passed, failed, flaky_test_names).
        - passed = tests - failures - errors (across all <testsuite> elements)
        - failed = sum of <testsuite failures> attribute
        - flaky  = "{classname}.{name}" for testcases with <failure> or <error> children
        """
        if not junit_path.exists():
            return 0, 0, []

        try:
            tree = ET.parse(junit_path)
        except ET.ParseError:
            return 0, 0, []

        root = tree.getroot()
        # JUnit XML may have <testsuites> root or a bare <testsuite> root.
        if root.tag == "testsuite":
            suites = [root]
        elif root.tag == "testsuites":
            suites = list(root.findall("testsuite"))
        else:
            return 0, 0, []

        total_tests = 0
        total_failures = 0
        total_errors = 0
        flaky: list[str] = []

        for suite in suites:
            try:
                total_tests += int(suite.attrib.get("tests", "0"))
                total_failures += int(suite.attrib.get("failures", "0"))
                total_errors += int(suite.attrib.get("errors", "0"))
            except ValueError:
                # Skip malformed suites rather than crashing the stage.
                continue

            for testcase in suite.findall("testcase"):
                if (
                    testcase.find("failure") is not None
                    or testcase.find("error") is not None
                ):
                    classname = testcase.attrib.get("classname", "")
                    name = testcase.attrib.get("name", "")
                    flaky.append(f"{classname}.{name}")

        passed = total_tests - total_failures - total_errors
        failed = total_failures
        return passed, failed, flaky

    @staticmethod
    def _parse_coverage(coverage_path: Path) -> float:
        """Parse pytest-cov's Cobertura XML and return coverage as a percent.

        Reads `line-rate` (0.0–1.0) from the <coverage> root and multiplies by 100.
        """
        if not coverage_path.exists():
            return 0.0

        try:
            tree = ET.parse(coverage_path)
        except ET.ParseError:
            return 0.0

        root = tree.getroot()
        line_rate = root.attrib.get("line-rate")
        if line_rate is None:
            return 0.0

        try:
            return float(line_rate) * 100
        except ValueError:
            return 0.0