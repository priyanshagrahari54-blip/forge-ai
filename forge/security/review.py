"""Independent review gate (A32.8).

Produces a structured ``ReviewDecision`` (APPROVE / REQUEST_CHANGES / BLOCK)
from the changed material. HIGH and CRITICAL findings block acceptance; the
decision is deterministic evidence, not a fabricated success. An optional
model-driven ``ReviewerAgent`` (``forge.agents.reviewer``) can contribute
findings through the Model Fabric, but this deterministic gate always runs and
its blockers always apply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class ReviewVerdict(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    BLOCK = "BLOCK"


class FindingSeverity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class ReviewFinding:
    severity: FindingSeverity
    message: str
    file: str = ""
    rule: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "message": self.message,
            "file": self.file,
            "rule": self.rule,
        }


@dataclass
class ReviewDecision:
    verdict: ReviewVerdict
    findings: list[ReviewFinding] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict == ReviewVerdict.APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "approved": self.approved,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
            "findings": [finding.to_dict() for finding in self.findings],
        }


#: Highest severity present, used to gate acceptance.
_SEVERITY_ORDER = {
    FindingSeverity.INFO: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}


def _verdict_for(findings: list[ReviewFinding]) -> ReviewVerdict:
    if not findings:
        return ReviewVerdict.APPROVE
    worst = max((_SEVERITY_ORDER[f.severity] for f in findings), default=0)
    if worst >= _SEVERITY_ORDER[FindingSeverity.HIGH]:
        return ReviewVerdict.BLOCK
    if worst >= _SEVERITY_ORDER[FindingSeverity.MEDIUM]:
        return ReviewVerdict.REQUEST_CHANGES
    return ReviewVerdict.APPROVE


class ReviewGate:
    """Deterministic independent review over changed material."""

    CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})", re.M)
    INCOMPLETE_IMPL = re.compile(r"^\+\s*pass\s*$", re.M)
    DYNAMIC_EXEC = re.compile(r"\b(?:eval|exec)\s*\(")
    SHELL_EXEC = re.compile(r"subprocess\.(?:run|Popen|call)\([^\n]*shell\s*=\s*True", re.I)
    TEST_WEAKENING = re.compile(r"\b(?:assert\s+True|pytest\.skip)\b", re.I)
    TRAVERSAL = re.compile(r"(?:^|[\s\"'])(?:\.\.(?:[/\\])|os\.path\.join\([^\n]*\.\.)")
    NETWORK_ACCESS = re.compile(r"\b(?:requests\.(?:get|post|put|delete|patch)|urllib\.request\.urlopen|socket\.socket)\s*\(")

    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def _material(self, diff: str, changed_files: Iterable[str]) -> tuple[str, list[ReviewFinding]]:
        material = diff
        findings: list[ReviewFinding] = []
        for name in changed_files:
            path = self.root / name
            if not path.is_file():
                continue
            try:
                material += f"\nFILE {name}\n{path.read_text(encoding='utf-8')}"
            except (OSError, UnicodeDecodeError):
                findings.append(ReviewFinding(FindingSeverity.HIGH, f"unreadable changed file: {name}", file=name, rule="unreadable"))
        return material, findings

    def review(self, diff: str = "", changed_files: Iterable[str] | None = None,
               *, requirement: str = "", model_findings: Iterable[ReviewFinding] | None = None) -> ReviewDecision:
        changed = list(changed_files or [])
        material, findings = self._material(diff, changed)

        if changed and not material.strip():
            findings.append(ReviewFinding(FindingSeverity.HIGH, "review received no changed material", rule="no-material"))
        if self.CONFLICT_MARKER.search(material):
            findings.append(ReviewFinding(FindingSeverity.CRITICAL, "conflict marker present in changed material", rule="conflict-marker"))
        if self.INCOMPLETE_IMPL.search(material):
            findings.append(ReviewFinding(FindingSeverity.HIGH, "incomplete implementation (pass stub)", rule="incomplete-impl"))
        if self.DYNAMIC_EXEC.search(material):
            findings.append(ReviewFinding(FindingSeverity.HIGH, "dynamic code execution", rule="dynamic-exec"))
        if self.SHELL_EXEC.search(material):
            findings.append(ReviewFinding(FindingSeverity.HIGH, "shell execution with shell=True", rule="shell-exec"))
        if self.TRAVERSAL.search(material):
            findings.append(ReviewFinding(FindingSeverity.HIGH, "path traversal in changed material", rule="traversal"))
        if self.NETWORK_ACCESS.search(material):
            findings.append(ReviewFinding(FindingSeverity.LOW, "network access introduced in changed code", rule="network-access"))

        # Test weakening is checked per changed test file so an ``assert True``
        # or ``pytest.skip`` is attributed precisely (the file path and the
        # offending line are usually on different lines of the material).
        for name in changed:
            path = self.root / name
            if not path.is_file():
                continue
            parts = Path(name).parts
            if "test" not in path.name.lower() and "tests" not in parts and "test" not in parts:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if self.TEST_WEAKENING.search(content):
                findings.append(ReviewFinding(
                    FindingSeverity.HIGH, "suspicious test weakening", file=name, rule="test-weakening",
                ))

        # Model-driven findings (optional) are merged; they never soften the
        # deterministic blockers above.
        for finding in model_findings or []:
            findings.append(finding)

        verdict = _verdict_for(findings)
        reason = {
            ReviewVerdict.APPROVE: "no blocking review findings",
            ReviewVerdict.REQUEST_CHANGES: "review requested changes",
            ReviewVerdict.BLOCK: "review blocked acceptance",
        }[verdict]
        return ReviewDecision(verdict=verdict, findings=findings, changed_files=changed, reason=reason)
