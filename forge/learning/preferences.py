"""Preference learning (A84 Stage I2).

Observes user corrections and stated preferences (through the memory
service's events, not by sweeping every message) and produces *proposed*
profile updates. A proposal only becomes a profile fact when the user
applies it (or an approved assistant turn does, via the gated memory path).

This closes the loop the addendum requires — preferred style, tools,
workflows, output formats and project conventions — while preserving user
control (C3): every proposal is inspectable, and forgetting takes a
proposal and its applied fact with it. Nothing here stores raw chat
history, and nothing here may touch safety-relevant settings: profile
fields are constrained to a closed vocabulary, so "preferred tool" can
never learn its way into "preferred permission".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["PreferenceObserver", "PreferenceProposal", "PROFILE_FIELDS"]

#: The closed field vocabulary a learned preference may ever target.
#: Anything security-, policy-, approval- or capability-shaped is refused.
PROFILE_FIELDS = ("response_style", "verbosity", "output_format",
                  "preferred_tools", "preferred_models", "workflows",
                  "project_conventions")

#: Observation → field mapping (deterministic keyword rules).
_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("be brief", "terse", "short answer", "concise", "tl;dr"), "verbosity"),
    (("in detail", "deep dive", "elaborate", "explain thoroughly",
      "thorough"), "verbosity"),
    (("json", "table", "bullet list", "markdown", "plain text"),
     "output_format"),
    (("i prefer", "i like", "always use", "use instead"), "response_style"),
    (("workflow", "routine", "every time", "when i ask", "from now on"),
     "workflows"),
    (("convention", "naming", "style guide", "we use"), "project_conventions"),
)

FORBIDDEN_MARKERS = ("permission", "policy", "approval", "bypass", "allow-all",
                     "auto-approve", "autoapprove", "sudo", "credential",
                     "password", "secret", "override", "security off",
                     "disable")


@dataclass(frozen=True)
class PreferenceProposal:
    field: str
    value: str
    reason: str
    evidence: str            # memory id / quote hash the proposal cites
    created_at: float
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "value": self.value, "reason": self.reason,
                "evidence": self.evidence, "created_at": self.created_at,
                "confidence": round(self.confidence, 4)}


class PreferenceObserver:
    """Turn preference-shaped *memory events* into closed-vocabulary proposals."""

    def __init__(self, *, require_consent: bool = True) -> None:
        #: When consent is required, proposals are recorded for the user to
        #: confirm; automatic application is never available in this build.
        self.require_consent = bool(require_consent)
        self._proposals: List[PreferenceProposal] = []
        self._rejected: List[Dict[str, str]] = []

    # -- observation -----------------------------------------------------------

    def observe(self, text: str, *, memory_id: str = "",
                confidence: float = 0.5) -> Tuple[PreferenceProposal, ...]:
        """Extract at most one proposal per matched field; refuse unsafe targets."""
        lowered = (text or "").lower()
        out: List[PreferenceProposal] = []
        seen_fields = {p.field for p in self._proposals}
        if any(marker in lowered for marker in FORBIDDEN_MARKERS):
            self._rejected.append({
                "reason": "preference text targets security/policy behaviour; "
                          "personalization can never override it",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return ()
        for markers, field in _RULES:
            if field in seen_fields:
                continue
            matched = next((m for m in markers if m in lowered), "")
            if not matched:
                continue
            value = _normalize_value(field, lowered, matched)
            proposal = PreferenceProposal(
                field=field, value=value,
                reason=f"matched {matched!r} in user-stated preference",
                evidence=memory_id or "user",
                created_at=time.time(),
                confidence=max(0.2, min(0.9, float(confidence))))
            self._proposals.append(proposal)
            out.append(proposal)
        return tuple(out)

    # -- proposals -------------------------------------------------------------

    def pending(self) -> Tuple[PreferenceProposal, ...]:
        return tuple(self._proposals)

    def reject_reasons(self) -> Tuple[Dict[str, str], ...]:
        return tuple(self._rejected)

    def forget(self, *, memory_id: str = "") -> int:
        """Drop proposals citing one memory id (or all). Returns count."""
        before = len(self._proposals)
        if memory_id:
            self._proposals = [p for p in self._proposals
                               if p.evidence != memory_id]
        else:
            self._proposals = []
        return before - len(self._proposals)

    def apply_allowed_fields(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Pure helper for the *user-confirmed* path: merge only known fields.

        The profile dict is the caller's; this never writes memory and never
        touches policy objects. Unknown keys in *profile* survive untouched;
        unknown keys in proposals are impossible by construction.
        """
        merged = dict(profile or {})
        for proposal in self._proposals:
            if proposal.field not in PROFILE_FIELDS:
                continue
            merged[proposal.field] = proposal.value
        return merged


def _normalize_value(field: str, lowered: str, marker: str) -> str:
    if field == "verbosity":
        if any(m in lowered for m in ("brief", "terse", "short", "tl;dr",
                                      "concise")):
            return "concise"
        return "detailed"
    if field == "output_format":
        for fmt in ("json", "table", "bullet list", "markdown", "plain text"):
            if fmt in lowered:
                return fmt
        return "text"
    tail = lowered.split(marker, 1)[-1].strip(" :.,-")
    return (tail[:120] or marker)[:120]
