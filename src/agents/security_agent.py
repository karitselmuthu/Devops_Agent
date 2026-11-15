"""SAST, dependency, and secret scans through Python subprocess wrappers."""

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel

from agents_shared.models import WorkflowTask


ScannerName = Literal["bandit", "trivy", "gitleaks"]


class Finding(BaseModel):
    scanner: ScannerName
    category: Literal["SAST", "SCA", "SECRET"]
    rule: str
    file: str
    line: int | None = None
    severity: str | None = None
    message: str


class ScanReport(BaseModel):
    outcome: Literal["clean", "blocked"]
    findings: list[Finding]
    scanners: list[ScannerName]


class SecurityFindingsError(Exception):
    """The scan ran successfully but found issues that block the workflow."""


class SecurityAgent:
    def __init__(
        self,
        repository: Path,
        scanners: tuple[ScannerName, ...] = ("bandit", "trivy", "gitleaks"),
        timeout_seconds: int = 300,
    ) -> None:
        self.repository = repository.resolve()
        if not self.repository.is_dir():
            raise ValueError("repository must be an existing directory")
        if not scanners:
            raise ValueError("at least one scanner is required")
        if any(scanner not in ("bandit", "trivy", "gitleaks") for scanner in scanners):
            raise ValueError("unsupported scanner")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.scanners = scanners
        self.timeout_seconds = timeout_seconds

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if shutil.which(command[0]) is None:
            raise RuntimeError(f"Scanner unavailable: {command[0]}")
        try:
            return subprocess.run(
                command,
                cwd=self.repository,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Scanner timed out: {command[0]}") from exc

    @staticmethod
    def _parse_json(text: str, scanner: str) -> object:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON report from {scanner}") from exc

    def _bandit(self) -> list[Finding]:
        result = self._run(
            ["bandit", "-r", "-f", "json", "-x", ".venv,.git", str(self.repository)]
        )
        if result.returncode not in (0, 1):
            raise RuntimeError("Bandit scan failed")
        data = self._parse_json(result.stdout, "bandit")
        if data.get("errors") or (result.returncode == 1 and not data.get("results")):
            raise RuntimeError("Bandit scan failed")
        return [
            Finding(
                scanner="bandit",
                category="SAST",
                rule=item.get("test_id", "unknown"),
                file=item.get("filename", ""),
                line=item.get("line_number"),
                severity=item.get("issue_severity"),
                message=item.get("issue_text", ""),
            )
            for item in data.get("results", [])
        ]

    def _trivy(self) -> list[Finding]:
        result = self._run(
            [
                "trivy", "fs", "--scanners", "vuln", "--format", "json",
                "--exit-code", "0", "--config", "", "--ignorefile", "",
                str(self.repository),
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("Trivy scan failed")
        data = self._parse_json(result.stdout, "trivy")
        findings: list[Finding] = []
        for target in data.get("Results", []):
            for item in target.get("Vulnerabilities") or []:
                findings.append(
                    Finding(
                        scanner="trivy",
                        category="SCA",
                        rule=item.get("VulnerabilityID", "unknown"),
                        file=target.get("Target", ""),
                        severity=item.get("Severity"),
                        message=item.get("Title") or item.get("PkgName", ""),
                    )
                )
        return findings

    def _gitleaks(self) -> list[Finding]:
        with TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "gitleaks.json"
            result = self._run(
                [
                    "gitleaks", "dir", str(self.repository), "--report-format", "json",
                    "--report-path", str(report_path), "--redact", "100",
                    "--no-banner",
                ]
            )
            if result.returncode not in (0, 1):
                raise RuntimeError("Gitleaks scan failed")
            if not report_path.exists():
                if result.returncode == 0:
                    return []
                raise RuntimeError("Gitleaks reported an error without a report")
            data = self._parse_json(report_path.read_text(), "gitleaks")
            if result.returncode == 1 and not data:
                raise RuntimeError("Gitleaks scan failed")
        return [
            Finding(
                scanner="gitleaks",
                category="SECRET",
                rule=item.get("RuleID", "unknown"),
                file=item.get("File", ""),
                line=item.get("StartLine"),
                message=item.get("Description", "Potential secret"),
            )
            for item in data
        ]

    def execute(self, task: WorkflowTask) -> str:
        if task.agent != "SECURITY":
            raise ValueError("SecurityAgent received a non-security task")
        methods = {"bandit": self._bandit, "trivy": self._trivy, "gitleaks": self._gitleaks}
        findings = [finding for scanner in self.scanners for finding in methods[scanner]()]
        report = ScanReport(
            outcome="blocked" if findings else "clean",
            findings=findings,
            scanners=list(self.scanners),
        )
        output = report.model_dump_json()
        if findings:
            raise SecurityFindingsError(output)
        return output
