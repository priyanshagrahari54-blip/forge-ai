"""Deterministic context budgeting for inference (Session 11).

A model has a real context limit and a repository does not fit in it. This
module turns "what we know" into "what we send" deterministically:

1. **Budget** — the model's context limit minus the reserved output tokens,
   minus the required sections (system, task, prompt, constraints). A limit
   the backend never reported is *assumed* conservatively and the assumption
   is recorded, never hidden.
2. **Rank** — sections are ordered by kind priority, then relevance score,
   then key. Ranking is total, so the same inputs always produce the same
   plan.
3. **Compress** — a section that does not fit is compressed (head + tail with
   an explicit elision marker) before it is dropped.
4. **Drop** — the lowest-value sections are dropped, and every omission is
   recorded with its reason and size.
5. **Refuse** — when even the required sections do not fit, the plan is
   reported infeasible rather than silently truncated into nonsense.

It integrates with the existing intelligence layer: :meth:`from_context_pack`
converts a :class:`~forge.intelligence.context.ContextPack` into scored
sections, and the token estimator matches the conservative posture of
:class:`~forge.intelligence.budget.ContextBudgetManager`.

Whole repositories are never dumped into a model, and nothing here touches the
network or a permission.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ASSUMED_CONTEXT_LIMIT",
    "ContextBudgetPlanner",
    "ContextPlan",
    "ContextSection",
    "KIND_PRIORITY",
]

#: Conservative chars-per-token estimate (matches the intelligence layer's
#: deliberately cheap, deterministic posture).
CHARS_PER_TOKEN = 4
#: Used when a backend reports no context limit. Recorded as an assumption.
ASSUMED_CONTEXT_LIMIT = 4096
#: Per-section cap so one huge file cannot eat the whole budget.
DEFAULT_MAX_SECTION_TOKENS = 1024
#: Hard cap on how many sections are ever sent.
DEFAULT_MAX_SECTIONS = 24

#: Ordering of section kinds (higher wins). Deterministic and documented.
KIND_PRIORITY: Dict[str, int] = {
    "system": 100,
    "prompt": 95,
    "task": 90,
    "instructions": 80,
    "constraints": 78,
    "repository": 60,
    "research": 50,
    "memory": 40,
    "attachment": 30,
}

#: Kinds that may be compressed/dropped when the budget is tight.
COMPRESSIBLE_KINDS: Tuple[str, ...] = ("repository", "research", "memory",
                                       "attachment", "instructions")

ELISION = "\n...[forge: %d chars elided]...\n"


@dataclass
class ContextSection:
    """One labelled piece of context."""

    key: str
    kind: str
    text: str
    score: float = 0.0
    #: Required sections are never dropped; if they do not fit the plan is
    #: infeasible (an honest refusal beats a silently broken prompt).
    required: bool = False
    #: Estimated tokens, filled in by the planner.
    tokens: int = 0
    #: Set when the planner compressed this section.
    compressed: bool = False
    original_tokens: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 20)

    def rank(self) -> Tuple[int, float, str, str]:
        """Total order: required first, then priority, score, kind, key."""
        return (0 if self.required else 1, -self.priority, -float(self.score),
                self.kind, self.key)

    def to_dict(self, *, include_text: bool = False) -> Dict[str, Any]:
        payload = {
            "key": self.key,
            "kind": self.kind,
            "score": round(float(self.score), 4),
            "required": bool(self.required),
            "tokens": int(self.tokens or 0),
            "original_tokens": int(self.original_tokens or 0),
            "compressed": bool(self.compressed),
            "chars": len(self.text or ""),
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass
class ContextPlan:
    """What will be sent, what was left out, and why."""

    context_limit: int = 0
    assumed_limit: bool = False
    reserved_output_tokens: int = 0
    budget_tokens: int = 0
    estimated_tokens: int = 0
    feasible: bool = True
    reason: str = ""
    included: List[ContextSection] = field(default_factory=list)
    omitted: List[Dict[str, Any]] = field(default_factory=list)
    compressed: List[Dict[str, Any]] = field(default_factory=list)
    max_sections: int = DEFAULT_MAX_SECTIONS
    max_section_tokens: int = DEFAULT_MAX_SECTION_TOKENS

    @property
    def sections(self) -> List[ContextSection]:
        return list(self.included)

    @property
    def omitted_tokens(self) -> int:
        return int(sum(item.get("tokens", 0) for item in self.omitted))

    def render(self) -> str:
        """Render the plan as one labelled prompt (deterministic order)."""
        blocks: List[str] = []
        for section in self.included:
            label = section.kind.upper()
            if section.key and section.key != section.kind:
                label = "%s (%s)" % (label, section.key)
            blocks.append("%s\n%s" % (label, section.text))
        if self.omitted:
            summary = ", ".join(
                "%s[%s]" % (item.get("key", "?"), item.get("reason", "?"))
                for item in self.omitted[:8])
            blocks.append("OMITTED CONTEXT\n%d section(s) were left out to "
                          "stay inside the model context limit: %s%s"
                          % (len(self.omitted), summary,
                             " ..." if len(self.omitted) > 8 else ""))
        return "\n\n".join(blocks)

    def to_dict(self, *, include_text: bool = False) -> Dict[str, Any]:
        return {
            "context_limit": int(self.context_limit),
            "assumed_limit": bool(self.assumed_limit),
            "reserved_output_tokens": int(self.reserved_output_tokens),
            "budget_tokens": int(self.budget_tokens),
            "estimated_tokens": int(self.estimated_tokens),
            "omitted_tokens": self.omitted_tokens,
            "feasible": bool(self.feasible),
            "reason": self.reason[:400],
            "sections": len(self.included),
            "max_sections": int(self.max_sections),
            "max_section_tokens": int(self.max_section_tokens),
            "included": [section.to_dict(include_text=include_text)
                         for section in self.included],
            "omitted": [dict(item) for item in self.omitted],
            "compressed": [dict(item) for item in self.compressed],
            "chars": len(self.render()) if include_text else sum(
                len(section.text or "") for section in self.included),
        }


class ContextBudgetPlanner:
    """Builds a deterministic, bounded context plan."""

    def __init__(self, *, chars_per_token: int = CHARS_PER_TOKEN,
                 default_context_limit: int = ASSUMED_CONTEXT_LIMIT,
                 max_sections: int = DEFAULT_MAX_SECTIONS,
                 max_section_tokens: int = DEFAULT_MAX_SECTION_TOKENS,
                 output_reserve_ratio: float = 0.25,
                 min_section_tokens: int = 8) -> None:
        self.chars_per_token = max(1, int(chars_per_token or 1))
        self.default_context_limit = max(64, int(default_context_limit or 64))
        self.max_sections = max(1, int(max_sections or 1))
        self.max_section_tokens = max(
            int(min_section_tokens), int(max_section_tokens or 1))
        self.output_reserve_ratio = min(0.9, max(0.0,
                                                 float(output_reserve_ratio)))
        self.min_section_tokens = max(1, int(min_section_tokens or 1))

    # -- estimation ------------------------------------------------------

    def estimate_tokens(self, text: str) -> int:
        """Cheap deterministic estimate. Never claims precision."""
        if not text:
            return 0
        return max(1, (len(text) + self.chars_per_token - 1)
                   // self.chars_per_token)

    # -- conversion from the existing ContextPack system ------------------

    def from_context_pack(self, pack: Any, *, kind: str = "repository",
                          required: bool = False,
                          score_scale: float = 1.0) -> List[ContextSection]:
        """Convert a :class:`ContextPack` into ranked context sections."""
        sections: List[ContextSection] = []
        items = []
        sorter = getattr(pack, "sorted_items", None)
        if callable(sorter):
            items = list(sorter())
        else:
            items = list(getattr(pack, "items", []) or [])
        for item in items:
            path = str(getattr(item, "path", "") or "")
            symbol = getattr(item, "symbol", None)
            key = "%s%s" % (path, ("#%s" % symbol) if symbol else "")
            reason = str(getattr(item, "reason", "") or "")
            score = float(getattr(item, "score", 0.0) or 0.0) * float(score_scale)
            lines = ""
            start = getattr(item, "start_line", None)
            end = getattr(item, "end_line", None)
            if start is not None and end is not None:
                lines = " lines %s-%s" % (start, end)
            body = str(getattr(item, "text", "") or "")
            if not body:
                body = "%s%s%s" % (key, lines,
                                   ("\nwhy: %s" % reason) if reason else "")
            sections.append(ContextSection(
                key=key or "repository", kind=getattr(item, "kind", kind)
                if getattr(item, "kind", "") in KIND_PRIORITY else kind,
                text=body, score=score, required=required,
                metadata={"symbol": symbol or "", "reason": reason[:200],
                          "lines": lines.strip()}))
        return sections

    def from_research(self, evidence: Sequence[Any]) -> List[ContextSection]:
        """Convert research evidence into ranked sections (metadata-safe)."""
        sections: List[ContextSection] = []
        for index, item in enumerate(evidence or ()):
            if isinstance(item, str):
                text, key, score = item, "research-%d" % index, 0.0
            else:
                text = str(getattr(item, "text", "")
                           or getattr(item, "content", "") or item)
                key = str(getattr(item, "source", "")
                          or getattr(item, "url", "")
                          or "research-%d" % index)
                score = float(getattr(item, "score", 0.0) or 0.0)
            sections.append(ContextSection(
                key=key[:200], kind="research", text=text[:8000], score=score))
        return sections

    def from_memory(self, records: Sequence[Any]) -> List[ContextSection]:
        sections: List[ContextSection] = []
        for index, item in enumerate(records or ()):
            if isinstance(item, str):
                text, key, score = item, "memory-%d" % index, 0.0
            else:
                text = str(getattr(item, "content", "")
                           or getattr(item, "summary", "") or item)
                key = str(getattr(item, "id", "")
                          or getattr(item, "key", "")
                          or "memory-%d" % index)
                score = float(getattr(item, "score", 0.0) or 0.0)
            sections.append(ContextSection(
                key=key[:200], kind="memory", text=text[:4000], score=score))
        return sections

    # -- planning --------------------------------------------------------

    def plan(self, sections: Sequence[ContextSection], *,
             context_limit: int = 0,
             max_output_tokens: Optional[int] = None,
             reserve_output_tokens: Optional[int] = None) -> ContextPlan:
        """Build the deterministic plan for one request."""
        limit = int(context_limit or 0)
        assumed = limit <= 0
        if assumed:
            limit = self.default_context_limit

        # A caller may ask for more output tokens than the model has context;
        # reserving the whole request would leave nothing to reason with, so
        # the reservation is capped at a fraction of the real limit.
        fraction = max(self.output_reserve_ratio, 0.25)
        reserved_output = 0
        if reserve_output_tokens is not None:
            reserved_output = max(0, int(reserve_output_tokens))
        elif max_output_tokens:
            reserved_output = max(0, int(max_output_tokens))
        else:
            reserved_output = int(limit * self.output_reserve_ratio)
        reserved_output = min(reserved_output, max(1, int(limit * fraction)))
        reserved_output = min(reserved_output, max(0, limit - 1))
        budget = max(0, limit - reserved_output)

        ranked = sorted((_prepare(section, self) for section in sections),
                        key=lambda section: section.rank())

        result = ContextPlan(
            context_limit=limit, assumed_limit=assumed,
            reserved_output_tokens=reserved_output, budget_tokens=budget,
            max_sections=self.max_sections,
            max_section_tokens=self.max_section_tokens)

        used = 0
        for section in ranked:
            if len(result.included) >= self.max_sections:
                result.omitted.append({
                    "key": section.key, "kind": section.kind,
                    "tokens": section.tokens, "score": section.score,
                    "reason": "section-cap"})
                continue
            cost = section.tokens
            if used + cost <= budget:
                used += cost
                result.included.append(section)
                continue
            # Compress before dropping (compressible kinds only).
            room = budget - used
            if section.kind in COMPRESSIBLE_KINDS \
                    and room >= self.min_section_tokens:
                compressed = _compress(section, room, self)
                if compressed is not None:
                    used += compressed.tokens
                    result.included.append(compressed)
                    result.compressed.append({
                        "key": compressed.key, "kind": compressed.kind,
                        "from_tokens": compressed.original_tokens,
                        "to_tokens": compressed.tokens,
                        "reason": "compressed-to-fit"})
                    continue
            result.omitted.append({
                "key": section.key, "kind": section.kind,
                "tokens": section.tokens, "score": section.score,
                "reason": ("over-budget" if used < budget
                           else "budget-exhausted")})

        result.estimated_tokens = used
        required_tokens = sum(section.tokens for section in ranked
                              if section.required)
        if required_tokens > budget:
            result.feasible = False
            result.reason = (
                "required context needs %d tokens but the budget is %d "
                "(context_limit=%d%s, reserved_output=%d)"
                % (required_tokens, budget, limit,
                   " assumed" if assumed else "", reserved_output))
        elif assumed:
            result.reason = ("context limit was not reported by the backend; "
                             "assumed %d tokens" % limit)
        elif result.omitted:
            result.reason = ("%d section(s) omitted to stay within %d tokens"
                             % (len(result.omitted), budget))
        else:
            result.reason = "all ranked sections fit the budget"
        return result

    def plan_for_request(self, *, prompt: str = "", task: str = "",
                         system: str = "", instructions: str = "",
                         context: str = "",
                         pack: Any = None,
                         research: Sequence[Any] = (),
                         memory: Sequence[Any] = (),
                         extra: Sequence[ContextSection] = (),
                         context_limit: int = 0,
                         max_output_tokens: Optional[int] = None
                         ) -> ContextPlan:
        """Convenience: build the canonical section set, then plan it."""
        sections: List[ContextSection] = []
        if system:
            sections.append(ContextSection(key="system", kind="system",
                                           text=system, required=True,
                                           score=1.0))
        if task:
            sections.append(ContextSection(key="task", kind="task", text=task,
                                           required=True, score=1.0))
        if instructions:
            sections.append(ContextSection(key="constraints",
                                           kind="instructions",
                                           text=instructions, score=0.9))
        if pack is not None:
            sections.extend(self.from_context_pack(pack))
        if context:
            sections.append(ContextSection(key="repository-context",
                                           kind="repository", text=context,
                                           score=0.5))
        sections.extend(self.from_research(research))
        sections.extend(self.from_memory(memory))
        sections.extend(extra)
        if prompt:
            sections.append(ContextSection(key="prompt", kind="prompt",
                                           text=prompt, required=True,
                                           score=1.0))
        return self.plan(sections, context_limit=context_limit,
                         max_output_tokens=max_output_tokens)


def _prepare(section: ContextSection, planner: ContextBudgetPlanner
             ) -> ContextSection:
    """Attach a token estimate and enforce the per-section cap."""
    tokens = planner.estimate_tokens(section.text)
    section.original_tokens = tokens or section.original_tokens
    section.tokens = tokens
    if tokens > planner.max_section_tokens and \
            section.kind in COMPRESSIBLE_KINDS:
        capped = _compress(section, planner.max_section_tokens, planner)
        if capped is not None:
            return capped
    return section


def _compress(section: ContextSection, budget_tokens: int,
              planner: ContextBudgetPlanner) -> Optional[ContextSection]:
    """Keep head + tail with an explicit elision marker.

    Returns ``None`` when the budget is too small to be useful, so the caller
    drops the section instead of sending a misleading fragment.
    """
    if budget_tokens < planner.min_section_tokens:
        return None
    budget_chars = max(planner.min_section_tokens * planner.chars_per_token,
                       budget_tokens * planner.chars_per_token)
    text = section.text or ""
    if len(text) <= budget_chars:
        return None
    marker_chars = 64
    usable = max(planner.min_section_tokens * planner.chars_per_token,
                 budget_chars - marker_chars)
    head = (usable * 2) // 3
    tail = usable - head
    elided = len(text) - head - tail
    body = text[:head] + (ELISION % max(0, elided)) + \
        text[len(text) - tail:] if tail else text[:head] + (ELISION % max(0, elided))
    compressed = ContextSection(
        key=section.key, kind=section.kind, text=body, score=section.score,
        required=False,      # a compressed section is never "required"
        tokens=planner.estimate_tokens(body),
        original_tokens=section.original_tokens or section.tokens,
        compressed=True, metadata=dict(section.metadata))
    compressed.metadata["elided_chars"] = max(0, elided)
    if compressed.tokens > budget_tokens:
        return None
    return compressed
