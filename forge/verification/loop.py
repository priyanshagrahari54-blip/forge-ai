"""generate → critique → revise → verify (A84 Stage K loop).

For high-impact tasks the assistant core routes output through this loop.
The loop owns *sequence and bounds*; it owns no authority:

* ``generate`` and ``revise`` are caller-supplied callables (so the loop can
  drive coder agents, model teams or plain fabric calls without knowing
  what any of them are);
* every critique comes from the deterministic-first :class:`Critic`;
* revision happens at most ``max_revisions`` times, and each iteration is
  recorded in the outcome — the loop's history is auditable evidence, not a
  black box;
* a final ``BLOCK`` verdict is honoured: the outcome is returned with
  ``accepted=False`` and the blocking findings. The loop can *never* mark
  its own output verified by trying again — an exhausted loop ends
  UNVERIFIED, which is honest, not ``ACCEPTED_WITH_RESERVATIONS``;
* verification (claims → evidence tracing) runs on the final artifact only;
  UNVERIFIED claims survive into the outcome for the user to see.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.verification.critic import Critic, CritiqueVerdict
from forge.verification.verifier import EvidenceVerifier

__all__ = ["CritiqueLoop", "LoopOutcome"]

MAX_REVISIONS_FLOOR = 0
MAX_REVISIONS_CEILING = 3


@dataclass(frozen=True)
class LoopIteration:
    index: int
    stage: str                      # generated | critiqued | revised | blocked
    verdict: str = ""
    finding_count: int = 0
    detail: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "stage": self.stage,
                "verdict": self.verdict, "finding_count": self.finding_count,
                "detail": self.detail[:200], "at": self.at}


@dataclass(frozen=True)
class LoopOutcome:
    artifact: str
    accepted: bool
    verified: bool
    verdict: str
    iterations: Tuple[LoopIteration, ...] = ()
    findings: Tuple[Dict[str, Any], ...] = ()
    claim_checks: Tuple[Dict[str, Any], ...] = ()
    code_files: Dict[str, str] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()

    @property
    def exhausted_unverified(self) -> bool:
        return (not self.accepted and self.verdict == CritiqueVerdict.REVISE)

    def to_dict(self) -> Dict[str, Any]:
        return {"artifact_preview": self.artifact[:1200],
                "artifact_chars": len(self.artifact),
                "accepted": self.accepted, "verified": self.verified,
                "verdict": self.verdict,
                "iterations": [item.to_dict() for item in self.iterations],
                "findings": list(self.findings),
                "claim_checks": list(self.claim_checks),
                "code_files": sorted(self.code_files),
                "notes": list(self.notes),
                "honesty": ("revise loops bound retries; they do not create "
                            "evidence")}


class CritiqueLoop:
    """Bounded critique/revise/verify cycle around any generator."""

    def __init__(self, critic: Optional[Critic] = None,
                 verifier: Optional[EvidenceVerifier] = None,
                 *, max_revisions: int = 1) -> None:
        self.critic = critic or Critic()
        self.verifier = verifier or EvidenceVerifier()
        self.max_revisions = max(MAX_REVISIONS_FLOOR,
                                 min(int(max_revisions), MAX_REVISIONS_CEILING))

    def run(self, *, generate: Callable[[], Any],
            revise: Optional[Callable[[str, Sequence[Dict[str, Any]]], Any]] = None,
            enhanced: Any = None,
            evidence: Sequence[Dict[str, Any]] = (),
            memory_hits: Sequence[Any] = (),
            claims: Sequence[str] = ()) -> LoopOutcome:
        iterations: List[LoopIteration] = []
        produced = _normalize(generate())
        artifact = produced.get("text", "")
        code_files = produced.get("files", {})
        iterations.append(LoopIteration(index=0, stage="generated",
                                        detail=f"{len(artifact)} chars"))
        report = self.critic.review(
            artifact, enhanced=enhanced, evidence=evidence,
            memory_hits=memory_hits, code_files=code_files or None)
        iterations.append(LoopIteration(
            index=0, stage="critiqued", verdict=report.verdict,
            finding_count=len(report.findings)))

        attempts = 0
        while report.verdict == CritiqueVerdict.REVISE and revise is not None \
                and attempts < self.max_revisions:
            attempts += 1
            findings = [f.to_dict() for f in report.findings]
            produced = _normalize(revise(artifact, findings))
            artifact = produced.get("text", "")
            code_files = produced.get("files", code_files)
            iterations.append(LoopIteration(
                index=attempts, stage="revised",
                detail=f"revision {attempts}/{self.max_revisions}"))
            report = self.critic.review(
                artifact, enhanced=enhanced, evidence=evidence,
                memory_hits=memory_hits, code_files=code_files or None)
            iterations.append(LoopIteration(
                index=attempts, stage="critiqued", verdict=report.verdict,
                finding_count=len(report.findings)))

        if report.verdict == CritiqueVerdict.BLOCK:
            iterations.append(LoopIteration(index=attempts, stage="blocked",
                                            verdict=report.verdict,
                                            detail="deterministic critical "
                                                   "finding"))
            return LoopOutcome(
                artifact=artifact, accepted=False, verified=False,
                verdict=report.verdict, iterations=tuple(iterations),
                findings=tuple(f.to_dict() for f in report.findings),
                code_files=code_files,
                notes=("output blocked by the critic; it is returned for "
                       "transparency but must not be presented as accepted",))

        claim_checks: Tuple[Dict[str, Any], ...] = ()
        if claims:
            contradictions = [{"topic": ref} for ref in (
                f.ref for f in report.findings
                if f.kind == "memory-contradiction") if ref]
            findings = self.verifier.verify_claims(
                claims, evidence=evidence, contradictions=contradictions)
            claim_checks = tuple(f.to_dict() for f in findings)
        verified = report.approved and bool(claim_checks) and all(
            f["status"] == "VERIFIED" for f in claim_checks)
        accepted = report.approved or (
            report.verdict == CritiqueVerdict.REVISE and not claim_checks)
        # A REVISE verdict after exhausting revisions is NOT accepted.
        if report.verdict == CritiqueVerdict.REVISE and attempts >= self.max_revisions \
                and (revise is not None or report.findings):
            accepted = False
        notes: List[str] = []
        if claim_checks:
            unverified = sum(1 for f in claim_checks
                             if f["status"] != "VERIFIED")
            notes.append(f"{len(claim_checks) - unverified}/{len(claim_checks)} "
                         "claims traced to evidence")
        if report.verdict == CritiqueVerdict.REVISE and not accepted:
            notes.append("revisions exhausted with open findings — output "
                         "is UNVERIFIED, not silently shipped")
        return LoopOutcome(
            artifact=artifact, accepted=bool(accepted), verified=verified,
            verdict=report.verdict, iterations=tuple(iterations),
            findings=tuple(f.to_dict() for f in report.findings),
            claim_checks=claim_checks, code_files=code_files,
            notes=tuple(notes))


def _normalize(value: Any) -> Dict[str, Any]:
    """Accept str, {text,files}, or coder-style dicts uniformly."""
    if isinstance(value, str):
        return {"text": value, "files": {}}
    if isinstance(value, dict):
        if "text" in value or "files" in value:
            return {"text": str(value.get("text", "")),
                    "files": dict(value.get("files") or {})}
        changes = value.get("changes")
        if isinstance(changes, dict):
            return {"text": str(value.get("summary", "")),
                    "files": {str(k): str(v) for k, v in changes.items()}}
        if isinstance(changes, list):
            files = {}
            for item in changes:
                if isinstance(item, dict) and item.get("path"):
                    files[str(item["path"])] = str(item.get("content", ""))
            return {"text": str(value.get("summary", "")), "files": files}
        return {"text": str(value), "files": {}}
    text = getattr(value, "text", None)
    if text is not None:
        return {"text": str(text), "files": {}}
    return {"text": str(value or ""), "files": {}}
