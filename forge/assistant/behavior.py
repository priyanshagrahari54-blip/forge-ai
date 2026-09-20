"""Assistant behavior policy (A84 Stage Q).

The behavioral contract, as a deterministic triage table — every branch is
implemented where the capability exists, and honestly refused where it does
not:

| situation                          | behavior                       |
|------------------------------------|--------------------------------|
| simple request                     | answer directly                |
| research required                  | research (deep engine)         |
| coding required                    | coding specialists + tools     |
| complex                            | plan + orchestrate             |
| genuinely ambiguous                | ask a useful clarification     |
| consequential                      | request confirmation           |
| insufficient evidence              | state what is unknown          |
| sources disagree                   | show the disagreement + both   |
| "continue …"                       | recover session/project state  |

Consequential operations are a *closed* verb set (deploy, delete, purge,
commit/push without review, credential handling, money/transactions). The
policy can only *add* friction (CONFIRM/CLARIFY) — it can never weaken an
A33 gate: confirmation here is the Stage-Q courtesy layer; the authorization
decision remains with the policy engine and the approval store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["Action", "AssistantBehavior", "TriageResult"]


class Action:
    DIRECT_ANSWER = "direct_answer"
    RESEARCH = "research"
    CODE = "code"
    ORCHESTRATE = "orchestrate"
    CLARIFY = "clarify"
    CONFIRM = "confirm"
    RECOVER_CONTINUE = "recover_continue"
    REPORT_UNKNOWN = "report_unknown"


#: verbs whose success mutates something dangerous or irreversible
_CONSEQUENTIAL = (
    "deploy", "production", "delete all", "delete everything", "drop table",
    "purge", "wipe", "erase", "rm -rf", "force push", "git push --force",
    "release to", "remove all", "nuke", "shred", "format the disk",
    "spend", "purchase", "charge", "send money", "transfer",
    "remove the database", "revoke all", "reset the keys", "rotate all",
)

#: mutating but routine engineering verbs (do not need Q-confirmation when
#: the A33 mode already gates writes — but they DO need the gates).
_CODE_VERBS = ("fix", "implement", "add ", "build", "refactor", "update",
               "create", "remove ", "delete the", "rename", "migrate",
               "optimize", "debug", "write ")

_RESEARCH_VERBS = ("research", "investigate", "compare", "evaluate",
                   "find sources", "what does the literature",
                   "latest", "documentation for", "how does .* work online",
                   "deep dive")

_CONTINUE_RE = re.compile(
    r"\b(continue|resume|go on|keep going|pick up where|the previous|"
    r"the earlier|last time|yesterday|go deeper)\b")


@dataclass(frozen=True)
class TriageResult:
    action: str
    reason: str
    confidence: float = 0.5
    needs_confirmation: bool = False
    confirmation_prompt: str = ""
    needs_clarification: bool = False
    clarification: str = ""
    signals: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason,
                "confidence": round(self.confidence, 3),
                "needs_confirmation": self.needs_confirmation,
                "confirmation_prompt": self.confirmation_prompt,
                "needs_clarification": self.needs_clarification,
                "clarification": self.clarification,
                "signals": list(self.signals),
                "honesty": "behavior policy adds friction only; it never "
                            "weakens an A33 authorization decision"}


class AssistantBehavior:
    """Deterministic triage: what kind of response does this request need?"""

    def __init__(self, *, repo_available: bool = True,
                 research_available: bool = True,
                 orchestration_available: bool = True) -> None:
        self.repo_available = bool(repo_available)
        self.research_available = bool(research_available)
        self.orchestration_available = bool(orchestration_available)

    def triage(self, message: str, *, enhanced: Any = None,
               continuity: Any = None, quality: Any = None,
               capabilities: Sequence[str] = ()) -> TriageResult:
        lowered = (message or "").lower().strip()
        caps = set(capabilities) or set(
            getattr(enhanced, "capabilities", ()) or ())
        signals: List[str] = []

        # 1. consequential -> confirm first, whatever else is true
        hits = tuple(m for m in _CONSEQUENTIAL if m in lowered)
        if hits:
            return TriageResult(
                Action.CONFIRM,
                "request matches consequential-operation vocabulary: "
                + ", ".join(hits[:4]),
                confidence=0.9, needs_confirmation=True,
                confirmation_prompt=(
                    "This can be destructive or irreversible. Confirm and I "
                    "will run it through the normal approval path — nothing "
                    "executes without the policy gate and, where required, "
                    "an approval you mint."),
                signals=("consequential:" + ",".join(hits),))

        # 2. unresolved continuity references -> recover (or ask)
        if _CONTINUE_RE.search(lowered):
            resolved = bool(getattr(continuity, "resolved", False))
            if getattr(continuity, "referenced", False) and resolved:
                return TriageResult(
                    Action.RECOVER_CONTINUE,
                    "reference to earlier state resolved from session/memory",
                    confidence=0.85,
                    signals=("continuity:" + str(
                        getattr(continuity, "kind", "")),))
            return TriageResult(
                Action.CLARIFY,
                "reference to earlier state could not be resolved from real "
                "records", confidence=0.7, needs_clarification=True,
                clarification=str(getattr(
                    continuity, "clarifying_question",
                    "Which task/session/report should I continue from?"))[:400],
                signals=("continuity-unresolved",))

        # 3. important ambiguity -> ask instead of guessing (D3)
        if quality is not None and bool(getattr(quality, "asks_user", False)):
            questions = tuple(getattr(quality, "questions", ()) or ())
            return TriageResult(
                Action.CLARIFY,
                "prompt quality evaluator returned ASK_USER",
                confidence=0.8, needs_clarification=True,
                clarification=(questions[0] if questions else
                               "What exactly should change, and how will we "
                               "verify it worked?")[:400],
                signals=("quality-ask",))

        # 4. engineering task -> code (or orchestrate when complex)
        if any(verb in lowered for verb in _CODE_VERBS):
            complexity = float(getattr(enhanced, "metadata_complexity", 1.0)
                               or 1.0)
            subtasks = len(getattr(enhanced, "subtasks", ()) or ())
            if self.orchestration_available and (
                    complexity >= 4.0 or subtasks >= 3):
                return TriageResult(
                    Action.ORCHESTRATE,
                    f"engineering request with {subtasks} decomposition "
                    "signal(s) — plan and orchestrate through the existing "
                    "supervisor pipeline", confidence=0.8,
                    signals=("code", "complex"))
            return TriageResult(
                Action.CODE,
                "engineering request; route to coding specialists + tools "
                "behind the policy gate" if self.repo_available else
                "engineering request, but no repository is attached — the "
                "answer will be a plan, not a change",
                confidence=0.75, signals=("code",))

        # 5. research need
        if caps & {"research"} or any(
                re.search(verb, lowered) for verb in _RESEARCH_VERBS if
                ".*" in verb) or any(
                verb in lowered for verb in _RESEARCH_VERBS if ".*" not in verb):
            return TriageResult(
                Action.RESEARCH,
                "external evidence required" if self.research_available else
                "evidence required but the research layer reports no live "
                "sources; the reply will list what stays unknown",
                confidence=0.7, signals=("research",))

        # 6. plain question / chat -> direct answer
        if lowered.endswith("?") or re.match(
                r"^(what|why|how|when|where|who|can|does|is|are|explain|"
                r"tell me|compare|summarize)\b", lowered):
            return TriageResult(Action.DIRECT_ANSWER,
                                "answerable from current context or honestly "
                                "unknown", confidence=0.6,
                                signals=("question",))

        # 7. statements without a work verb: acknowledge, offer next step
        return TriageResult(Action.DIRECT_ANSWER,
                            "no action verb detected; respond directly and "
                            "keep the turn in session memory per policy",
                            confidence=0.5, signals=("chat",))

    # -- helpers used by core -------------------------------------------------------

    @staticmethod
    def evidence_report(text: str, evidence_count: int) -> Optional[str]:
        """When the retrieved evidence cannot support an answer, say so."""
        if evidence_count > 0:
            return None
        return ("What is unknown right now: no retrieved evidence supports a "
                "confident answer to this. I will not guess; here is what I "
                "checked: " + (text[:200] or "the current session") + ".")
