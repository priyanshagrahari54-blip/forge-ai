"""Personalization (A84 Stage L).

The profile adapts *how* Forge answers — explanation depth, communication
style, output format, recurring workflows, project conventions, preferred
tools and preferred models — while never touching *whether* Forge may do
something:

* profile fields are a closed vocabulary (``response_style``, ``verbosity``,
  ``output_format``, ``preferred_tools``, ``preferred_models``, ``workflows``,
  ``project_conventions``); a key shaped like a permission or policy knob is
  rejected at construction time, not silently stored;
* the profile is *persisted as memory* (type ``preference``, retention
  ``persistent``) so inspect/correct/delete/forget (C3) act on it through
  the same gated path as every other memory;
* preference into routing is a *soft input*: ``preferred_models`` becomes
  ``ModelRequest.preferred_models``, which the fabric still evaluates under
  capability, policy, health and verification checks — a preferred model
  that cannot serve the request is not used and the request is not relaxed;
  ``prefer_local``/``prefer_free`` can only ever be set to *stricter* than
  policy (personalization can ask for privacy, never waive it);
* prompt shaping (verbosity/format) reflows presentation only — the goal
  text is untouched, and the enhancement layer's intent-preservation check
  runs after personalization, not before it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["PreferenceProfile", "PROFILE_FIELD_NAMES", "parse_profile_memory"]

PROFILE_FIELD_NAMES = ("response_style", "verbosity", "output_format",
                       "preferred_tools", "preferred_models", "workflows",
                       "project_conventions")

#: Keys that personalization may never carry, whatever their value.
_FORBIDDEN_KEY_MARKERS = ("permission", "policy", "approval", "bypass",
                          "allow", "auto", "override", "security", "secret",
                          "credential", "sudo", "admin", "root")

MEMORY_PREFIX = "profile:"
_VALUE_SEP = "="

_VERBOSITY_STYLES = {
    "concise": ("Answer briefly: conclusion first, then at most 3 bullet "
                "points of support."),
    "detailed": ("Answer thoroughly: explain reasoning, trade-offs and edge "
                 "cases; include the evidence for each claim."),
}


def parse_profile_memory(content: str) -> Optional[Tuple[str, str]]:
    """Decode a ``profile:<field>=<value>`` memory line."""
    text = (content or "").strip()
    if not text.lower().startswith(MEMORY_PREFIX):
        return None
    body = text[len(MEMORY_PREFIX):]
    if _VALUE_SEP not in body:
        return None
    field_name, value = body.split(_VALUE_SEP, 1)
    field_name = field_name.strip().lower()
    value = value.strip()
    if field_name in PROFILE_FIELD_NAMES and value:
        return field_name, value[:200]
    return None


@dataclass
class PreferenceProfile:
    """The user's established presentation/routing preferences (soft)."""

    values: Dict[str, str] = field(default_factory=dict)
    source_memory_ids: Dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------------

    @classmethod
    def from_memory(cls, engine: Any, *, project: str = "personal",
                    limit: int = 200) -> "PreferenceProfile":
        """Load profile fields from the *engine*; last write per field wins."""
        profile = cls()
        try:
            rows = list(engine.list(project=project, memory_type="preference",
                                     limit=limit, include_inactive=False))
        except Exception:
            rows = []
        for record in rows:
            parsed = parse_profile_memory(str(getattr(record, "content", "")))
            if parsed is None:
                continue
            field_name, value = parsed
            profile.values[field_name] = value
            profile.source_memory_ids[field_name] = str(
                getattr(record, "id", ""))
        return profile

    # -- closed vocabulary ------------------------------------------------------------

    def set(self, field_name: str, value: str) -> None:
        field_name = (field_name or "").strip().lower()
        if field_name not in PROFILE_FIELD_NAMES:
            raise ValueError(
                f"profile field {field_name!r} is not personalizable; "
                f"allowed: {', '.join(PROFILE_FIELD_NAMES)}")
        lowered = field_name.lower()
        if any(marker in lowered for marker in _FORBIDDEN_KEY_MARKERS):
            raise ValueError("profile fields can never encode permission or "
                             "policy behaviour")
        text = str(value or "").strip()[:200]
        if not text:
            self.values.pop(field_name, None)
            return
        self.values[field_name] = text

    def get(self, field_name: str, default: str = "") -> str:
        return str(self.values.get(field_name, default) or default)

    def to_dict(self) -> Dict[str, Any]:
        return {"fields": dict(self.values),
                "storage": "memory (type=preference, retention=persistent) "
                           "behind the A37/A33 gated path",
                "never_overrides": ["safety", "security", "truthfulness",
                                     "authorization", "system constraints"],
                "note": "preferences are presentation + soft routing hints; "
                        "they can restrict, never relax"}

    # -- application hooks -----------------------------------------------------------

    def presentation_guidance(self) -> Tuple[str, ...]:
        """Lines appended to the *enhanced* prompt (presentation only)."""
        lines: List[str] = []
        verbosity = self.get("verbosity").lower()
        if verbosity in _VERBOSITY_STYLES:
            lines.append(_VERBOSITY_STYLES[verbosity])
        style = self.get("response_style")
        if style:
            lines.append("Response style: " + style[:160])
        output_format = self.get("output_format")
        if output_format:
            lines.append("Preferred output format (if compatible with the "
                         "request): " + output_format[:80])
        depth = self.get("explanation_depth")  # not a personalizable field
        del depth  # depth is expressed via verbosity; unknown keys impossible
        return tuple(lines)

    def apply_to_request(self, request: Any) -> Dict[str, Any]:
        """Attach soft model preferences to a ``ModelRequest`` in place.

        Only fields the fabric itself treats as soft (preferences) or as
        *stricter* policy (prefer_local/prefer_free) are set. Returns what
        was changed so the caller can record it in the prompt version.
        """
        changed: Dict[str, Any] = {}
        preferred = self._split("preferred_models")
        if preferred:
            request.preferred_models = tuple(preferred)
            changed["preferred_models"] = preferred
        tools = self._split("preferred_tools")
        if tools:
            changed["preferred_tools"] = tools     # planner hint, not policy
        if self.get("privacy_mode", "").lower() in ("strict", "local", "on"):
            current = bool(getattr(request, "prefer_local", True))
            request.prefer_local = True if current else True
            changed["prefer_local"] = True
        return changed

    def planner_hints(self) -> Dict[str, Any]:
        return {"preferred_tools": self._split("preferred_tools"),
                "workflows": self._split("workflows"),
                "project_conventions": self._split("project_conventions")}

    def _split(self, field_name: str) -> List[str]:
        raw = self.get(field_name)
        return [p.strip() for p in re.split(r"[,;|]", raw) if p.strip()][:8]

    # -- persistence ------------------------------------------------------------------

    def save_to(self, engine: Any, *, project: str = "personal",
                source: str = "user") -> Dict[str, Any]:
        """Persist each field as one ``profile:<field>=<value>`` memory.

        Correction (not duplication): an existing profile line for the same
        field is corrected through the engine so the version trail survives.
        """
        saved: Dict[str, str] = {}
        existing: Dict[str, str] = {}
        try:
            for record in engine.list(project=project,
                                      memory_type="preference", limit=200):
                parsed = parse_profile_memory(str(getattr(record, "content", "")))
                if parsed is not None:
                    existing[parsed[0]] = str(getattr(record, "id", ""))
        except Exception:
            existing = {}
        for field_name, value in self.values.items():
            if field_name not in PROFILE_FIELD_NAMES:
                continue
            content = f"{MEMORY_PREFIX}{field_name}{_VALUE_SEP}{value}"
            if field_name in existing:
                try:
                    engine.correct(existing[field_name], content,
                                   source=source, reason="profile update",
                                   project=project)
                    saved[field_name] = "corrected"
                    continue
                except Exception:
                    pass
            result = engine.remember("preference", content, project=project,
                                     source=source, via="personalization",
                                     confidence=0.8, importance=0.7,
                                     retention="persistent",
                                     metadata={"reason_for_retention":
                                               "profile field"})
            saved[field_name] = str(getattr(result, "status", "?"))
        return {"saved": saved, "fields": sorted(self.values)}


def merge_proposals(profile: PreferenceProfile, proposals: Iterable[Any]
                    ) -> PreferenceProfile:
    """Apply *user-confirmed* proposals only; returns a new profile.

    The helper itself is side-effect free — callers decide whether proposals
    count as confirmed (an explicit user action through the API).
    """
    merged = PreferenceProfile(values=dict(profile.values),
                               source_memory_ids=dict(profile.source_memory_ids))
    for proposal in proposals:
        field_name = getattr(proposal, "field", "")
        value = getattr(proposal, "value", "")
        if field_name in PROFILE_FIELD_NAMES and value:
            merged.set(str(field_name), str(value))
    return merged
