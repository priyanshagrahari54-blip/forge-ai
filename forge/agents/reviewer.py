from __future__ import annotations

import json
import re

from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.security.review import FindingSeverity, ReviewFinding


class ReviewerAgent:
    """Independent reviewer.

    ``build_context`` selects review context deterministically; ``review``
    optionally asks the Model Fabric (capability ``review``) for structured
    findings and falls back to ``None`` when no review-capable model is
    available, so callers always retain the deterministic ``ReviewGate`` as the
    mandatory blocker.
    """

    name = "reviewer"

    def __init__(self, fabric=None):
        self.fabric = fabric

    def describe(self) -> str:
        return "Responsible for independently reviewing changes."

    def build_context(
        self,
        intelligence: RepositoryIntelligence,
        task: str,
        target_files: tuple[str, ...] = (),
        target_symbols: tuple[str, ...] = (),
        max_tokens: int = 4000,
    ) -> AgentContext:
        return AgentContextBuilder(
            intelligence,
            max_tokens=max_tokens,
        ).build(
            task=task,
            target_files=target_files,
            target_symbols=target_symbols,
        )

    def review(
        self,
        task: str,
        diff: str = "",
        changed_files: tuple[str, ...] = (),
        memory=None,
        project: str | None = None,
    ) -> list[ReviewFinding] | None:
        """Ask the fabric for independent structured review findings.

        Returns a list of ``ReviewFinding`` when a review model answered, and
        ``None`` when no review model is available (the caller then relies on
        the deterministic gate). A malformed model response is ignored rather
        than trusted.

        ``memory``/``project`` are optional long-term-memory integration
        points: when supplied, HIGH/CRITICAL findings are remembered as
        durable decision memory for later tasks.
        """
        if self.fabric is None:
            return None
        from forge.models.request import ModelRequest

        prompt = (
            "Review the following change set for correctness, regressions, and "
            "security. Return ONLY JSON: "
            "{findings:[{severity:INFO|LOW|MEDIUM|HIGH|CRITICAL, message:str, "
            "file:str}], verdict:APPROVE|REQUEST_CHANGES|BLOCK}. "
            "HIGH or CRITICAL findings must block.\n"
            f"TASK: {task}\n"
            f"CHANGED FILES: {', '.join(changed_files)}\n"
            f"DIFF:\n{diff[:6000]}"
        )
        response = self.fabric.generate(ModelRequest(
            prompt=prompt,
            capability="review",
            required_capabilities=("review",),
            task=task,
            prefer_local=True,
            prefer_free=True,
        ))
        if not response.success:
            return None
        findings = self._parse(response.text)
        if memory is not None and findings:
            self._record_findings(findings, memory, project, task)
        return findings

    @staticmethod
    def _record_findings(findings, memory, project, task) -> None:
        """Remember HIGH/CRITICAL review findings as durable decisions."""
        from forge.memory.integrations import remember_decision
        from forge.security.review import FindingSeverity

        blocking = [finding for finding in findings
                    if finding.severity in (FindingSeverity.HIGH,
                                            FindingSeverity.CRITICAL)]
        for finding in blocking[:3]:
            remember_decision(
                memory, project or "default",
                f"review blocked on {task}: {finding.message}",
                source="reviewer", importance=0.8, confidence=0.7)

    @staticmethod
    def _parse(text: str) -> list[ReviewFinding]:
        cleaned = text.strip()
        if "```" in cleaned:
            cleaned = re.sub(r"```(?:json)?", "", cleaned).replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        findings: list[ReviewFinding] = []
        for item in data.get("findings", []):
            severity_raw = str(item.get("severity", "INFO")).upper()
            try:
                severity = FindingSeverity(severity_raw)
            except ValueError:
                severity = FindingSeverity.INFO
            findings.append(ReviewFinding(
                severity=severity,
                message=str(item.get("message", "")),
                file=str(item.get("file", "")),
                rule="model-review",
            ))
        return findings