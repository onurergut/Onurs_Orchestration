"""Security stage: runs bandit on the worktree and parses JSON output.

Pure subprocess + JSON parsing — no LLM call. Returns a `SecurityReport`.
Gracefully degrades to an empty report when `bandit` is not installed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

from pydantic import BaseModel

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew

# Severity ordering used to aggregate the report's `severity` list.
# Bandit reports LOW / MEDIUM / HIGH (uppercase); we lowercase them so the
# downstream aggregation is stable and human-readable.
_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class Finding(BaseModel):
    id: str
    cwe: str | None
    severity: str
    message: str
    file: str
    line: int

    # Tell pytest not to try to collect this BaseModel as a test class.
    __test__ = False


class SecurityReport(BaseModel):
    findings: list[Finding]
    severity: list[str]

    # Tell pytest not to try to collect this BaseModel as a test class.
    __test__ = False


class SecurityStage(StageCrew):
    name = "security"
    output_schema = SecurityReport

    # Tell pytest not to try to collect this stage class as a test class.
    __test__ = False

    def __init__(self, bandit_executable: str | None = None):
        if bandit_executable is None:
            bandit_executable = shutil.which("bandit") or ".venv/bin/bandit"
        self.bandit_executable = bandit_executable

    async def run(self, ctx: StageContext, llm: LLMClient) -> SecurityReport:
        cmd = [
            self.bandit_executable,
            "-r",
            ".",
            "-f",
            "json",
            "-q",
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=str(ctx.worktree_path),
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # bandit isn't installed — degrade gracefully rather than fail the run.
            return SecurityReport(findings=[], severity=[])

        return self._parse_report(proc.stdout)

    def _parse_report(self, stdout: str) -> SecurityReport:
        """Parse bandit's JSON output into a SecurityReport."""
        if not stdout.strip():
            # Bandit can emit an empty stdout when there's nothing to scan.
            return SecurityReport(findings=[], severity=[])

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return SecurityReport(findings=[], severity=[])

        raw_results = payload.get("results") or []
        findings: list[Finding] = []
        severity_set: set[str] = set()

        for issue in raw_results:
            severity_raw = str(issue.get("issue_severity", "")).lower()
            cwe_id = (issue.get("issue_cwe") or {}).get("id")
            finding = Finding(
                id=str(issue.get("test_id", "")),
                cwe=f"CWE-{cwe_id}" if cwe_id is not None else None,
                severity=severity_raw,
                message=str(issue.get("issue_text", "")),
                file=str(issue.get("filename", "")),
                line=int(issue.get("line_number", 0) or 0),
            )
            findings.append(finding)
            if severity_raw in _SEVERITY_ORDER:
                severity_set.add(severity_raw)

        severity = sorted(severity_set, key=lambda s: _SEVERITY_ORDER[s], reverse=True)
        return SecurityReport(findings=findings, severity=severity)
