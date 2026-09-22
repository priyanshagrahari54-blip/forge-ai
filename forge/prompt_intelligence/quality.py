"""Prompt quality evaluation before execution (A84 Stage D3).

The evaluator inspects an :class:`~forge.prompt_intelligence.pipeline.EnhancedPrompt`
(or raw text) and decides one of three verdicts:

* ``EXECUTE``       — clear enough to run; annotate minor gaps.
* ``ASK_USER``      — a *genuinely important* ambiguity exists; asking a
  concrete question is cheaper and safer than guessing.
* ``REJECT_INPUT``  — nothing executable (empty/garbage input).

The evaluator is deterministic and model-free: it reads structural signals
(blocks, contradictions, missing requirements, verification gaps, context
sufficiency). It never rewrites the request and never approves execution on
the strength of style alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["PromptQualityEvaluator", "QualityFlags", "QualityVerdict"]

#: Score floor at which we prefer a clarifying question over guessing.
ASK_THRESHOLD = 0.55


@dataclass(frozen=True)
class QualityFlags:
    """Independent structural signals; true means the signal was observed."""

    ambiguous: bool = False
    missing_requirements: bool = False
    conflicting_constraints: bool = False
    insufficient_context: bool = False
    output_format_unclear: bool = False
    verification_missing: bool = False
    sensitive_scope: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "ambiguous": self.ambiguous,
            "missing_requirements": self.missing_requirements,
            "conflicting_constraints": self.conflicting_constraints,
            "insufficient_context": self.insufficient_context,
            "output_format_unclear": self.output_format_unclear,
            "verification_missing": self.verification_missing,
            "sensitive_scope": self.sensitive_scope,
        }

    @property
    def blocking_count(self) -> int:
        return sum((self.ambiguous, self.conflicting_constraints,
                    self.missing_requirements))


@dataclass(frozen=True)
class QualityVerdict:
    verdict: str                    # EXECUTE | ASK_USER | REJECT_INPUT
    score: float                    # bounded 0..1, deterministic
    flags: QualityFlags = field(default_factory=QualityFlags)
    questions: tuple = ()           # concrete clarifying questions (ASK_USER)
    notes: tuple = ()               # annotations that never block execution
    #: Which signals drove the verdict — part of the audit trail.
    reasons: tuple = ()

    @property
    def asks_user(self) -> bool:
        return self.verdict == "ASK_USER"

    @property
    def executable(self) -> bool:
        return self.verdict == "EXECUTE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "flags": self.flags.to_dict(),
            "questions": list(self.questions),
            "notes": list(self.notes),
            "reasons": list(self.reasons),
        }


_QUESTION_BY_KIND = {
    "vague-objective": "What exactly should change, and how will we verify "
                       "it worked?",
    "unresolved-reference": "Which earlier item do you mean — a task, "
                            "session, project or file? (Give me a name or "
                            "just say 'the last one' and I will recover it.)",
    "conflicting-output-format": "You asked for incompatible output formats. "
                                 "Which one wins: JSON or a table?",
    "underspecified": "Which files/module should this touch, and what counts "
                      "as success?",
}

_SENSITIVE_MARKERS = ("credential", "password", "secret", "private key",
                      ".env", "delete all", "purge", "wipe")


class PromptQualityEvaluator:
    """Structural quality gate between enhancement and execution."""

    def evaluate(self, enhanced: Any = None, *, raw: str = "") -> QualityVerdict:
        if enhanced is None and raw:
            from forge.prompt_intelligence.pipeline import PromptIntelligence
            enhanced = PromptIntelligence().enhance(raw)
        if enhanced is None:
            return QualityVerdict(verdict="REJECT_INPUT", score=0.0,
                                  reasons=("no prompt supplied",))
        text = str(getattr(enhanced, "original", raw) or "").strip()
        if not text:
            return QualityVerdict(verdict="REJECT_INPUT", score=0.0,
                                  reasons=("empty request",))

        ambiguities = list(getattr(enhanced, "ambiguities", ()) or ())
        goals = tuple(getattr(enhanced, "goals", ()) or ())
        constraints = tuple(getattr(enhanced, "constraints", ()) or ())
        snippets = tuple(getattr(enhanced, "context_snippets", ()) or ())
        verification = tuple(getattr(enhanced, "verification", ()) or ())
        output_format = str(getattr(enhanced, "output_format", "text") or "text")
        intent_preserved = bool(getattr(enhanced, "intent_preserved", True))

        blocking = [a for a in ambiguities
                    if str(a.get("severity")) == "blocking"]
        flags = QualityFlags(
            ambiguous=bool(blocking),
            missing_requirements=not goals and not text.endswith("?"),
            conflicting_constraints=any(
                a.get("kind") == "conflicting-output-format"
                for a in ambiguities),
            insufficient_context=bool(blocking) and not snippets,
            output_format_unclear=output_format == "text" and len(text) < 40,
            verification_missing=not verification,
            sensitive_scope=any(m in text.lower() for m in _SENSITIVE_MARKERS))

        score = 1.0
        score -= 0.30 * len(blocking)
        if flags.missing_requirements:
            score -= 0.15
        if flags.insufficient_context:
            score -= 0.10
        if flags.output_format_unclear:
            score -= 0.05
        if not intent_preserved:
            # An enhancement that dropped user terms must never execute.
            score -= 1.0
        score = max(0.0, min(1.0, round(score, 4)))

        notes: List[str] = []
        reasons: List[str] = []
        questions: List[str] = []
        for item in blocking:
            kind = str(item.get("kind", ""))
            reason = _QUESTION_BY_KIND.get(kind, str(item.get("detail", "")))
            if reason not in questions:
                questions.append(reason)
            reasons.append(f"blocking ambiguity: {kind}")
        if flags.insufficient_context:
            notes.append("no retrieved context accompanied the request")
        if flags.sensitive_scope:
            notes.append("sensitive scope detected — consequential "
                         "confirmation required before acting")
            reasons.append("sensitive scope flagged")
        minor = [a for a in ambiguities
                 if str(a.get("severity")) != "blocking"]
        for item in minor:
            notes.append(f"annotated: {item.get('kind')} — "
                         f"{str(item.get('detail', ''))[:180]}")

        if not intent_preserved:
            return QualityVerdict(
                verdict="ASK_USER", score=0.0, flags=flags,
                questions=("the enhanced prompt no longer matches your "
                           "original objective; re-state it and I will "
                           "retry the enhancement",),
                notes=tuple(notes),
                reasons=("intent preservation check failed",))
        if blocking and score < ASK_THRESHOLD:
            return QualityVerdict(verdict="ASK_USER", score=score,
                                  flags=flags, questions=tuple(questions),
                                  notes=tuple(notes), reasons=tuple(reasons))
        return QualityVerdict(verdict="EXECUTE", score=score, flags=flags,
                              notes=tuple(notes), reasons=tuple(reasons))
