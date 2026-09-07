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
    ) -> list[ReviewFinding] | None:
        """Ask the fabric for independent structured review findings.

        Returns a list of ``ReviewFinding`` when a review model answered, and
        ``None`` when no review model is available (the caller then relies on
        the deterministic gate). A malformed model response is ignored rather
        than trusted.
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
        return self._parse(response.text)

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