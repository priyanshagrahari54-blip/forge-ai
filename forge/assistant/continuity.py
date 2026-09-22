"""Conversation continuity (A84 Stage B3).

Users say "continue that", "the previous project", "use what we discussed
yesterday", "go deeper", "compare this with the earlier result", "continue
from the last checkpoint" — and they should not have to repeat themselves.

This resolver maps such references onto real, retrievable state:

1. the session's active task (from the assistant session ledger),
2. the most recent referenced task/research/artifact (recency + kind),
3. memory recall over session/project layers (bounded, relevance-ranked —
   retrieval, never wholesale history injection),
4. explicit time references ("yesterday" → a 36h window before now).

If none of these resolve the reference, the resolver honestly returns
``resolved=False`` with a clarifying question — *asking* instead of guessing
(Stage D3/Q). It never fabricates a prior context and never attaches an
arbitrary old conversation to make a message look understood.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ["ContinuityBundle", "ContinuityResolver", "REFERENCE_PATTERNS"]

#: Reference patterns -> (target, depth) semantics.
REFERENCE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bcontinue (?:that|it|this|from|with)?\b", "active_task"),
    (r"\bresume (?:that|it|the)\b", "active_task"),
    (r"\bthe previous (?:project|session|task|one|result)\b", "previous"),
    (r"\bthe earlier (?:result|answer|finding|report)\b", "previous"),
    (r"\b(?:use )?what we discussed\b", "discussion"),
    (r"\byesterday\b", "yesterday"),
    (r"\blast (?:time|night|week)\b", "recent"),
    (r"\bgo deeper\b|\bdeeper\b|\bmore detail\b|\belaborate\b", "deeper"),
    (r"\bcompare (?:this|that|it)? ?(?:with|to) (?:the )?(?:earlier|previous|last)\b",
     "compare"),
    (r"\bfrom the last checkpoint\b|\blast checkpoint\b", "checkpoint"),
    (r"\bthe previous project\b|\bthat project\b", "project"),
)

_WINDOW = {"yesterday": (12 * 3600.0, 36 * 3600.0),
           "recent": (0.0, 7 * 24 * 3600.0)}


@dataclass(frozen=True)
class ContinuityBundle:
    """What was resolved, from where, and what is still unknown."""

    referenced: bool
    resolved: bool
    kind: str = ""
    items: Tuple[Dict[str, Any], ...] = ()
    #: Assembled text handed to the context engine (bounded by the caller).
    context_text: str = ""
    clarifying_question: str = ""
    missing: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"referenced": self.referenced, "resolved": self.resolved,
                "kind": self.kind, "items": [dict(i) for i in self.items],
                "context_chars": len(self.context_text),
                "clarifying_question": self.clarifying_question,
                "missing": list(self.missing), "notes": list(self.notes),
                "honesty": "unresolved references are asked about, never "
                           "invented"}


class ContinuityResolver:
    """Deterministic anaphora resolution over real session state."""

    def __init__(self, *, ledger: Any = None, memory: Any = None,
                 task_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        #: ``ledger``: SessionLedger (duck-typed get()/list()).
        #: ``memory``: PersonalMemoryService (duck-typed recall()).
        #: ``task_lookup``: optional resolver from task id -> live task dict
        #: (the plane provides it; tests provide a stub).
        self.ledger = ledger
        self.memory = memory
        self.task_lookup = task_lookup
        self._now = now or time.time

    # -- public ------------------------------------------------------------------

    @staticmethod
    def detect(message: str) -> List[str]:
        """Return matched reference kinds (empty => no continuity needed)."""
        lowered = (message or "").lower()
        kinds: List[str] = []
        for pattern, kind in REFERENCE_PATTERNS:
            if re.search(pattern, lowered) and kind not in kinds:
                kinds.append(kind)
        return kinds

    def resolve(self, session_id: str, message: str) -> ContinuityBundle:
        kinds = self.detect(message)
        if not kinds:
            return ContinuityBundle(referenced=False, resolved=True,
                                    kind="none")
        session = self.ledger.get(session_id) if self.ledger else None
        items: List[Dict[str, Any]] = []
        missing: List[str] = []
        notes: List[str] = []
        context_parts: List[str] = []
        resolved_any = False

        for kind in kinds:
            found = self._resolve_one(kind, session, message)
            if found:
                resolved_any = True
                for item in found:
                    if item not in items:
                        items.append(item)
                    rendered = _render(item)
                    if rendered and rendered not in context_parts:
                        context_parts.append(rendered)
            else:
                missing.append(kind)

        if not resolved_any:
            return ContinuityBundle(
                referenced=True, resolved=False, kind=",".join(kinds),
                clarifying_question=(
                    "I want to continue from real state rather than guess. "
                    "Which do you mean — a task id, a session, a project, "
                    "or a research report? (Recent sessions are listed in "
                    "the assistant view.)"),
                missing=tuple(missing),
                notes=("nothing matched the reference; the message will not "
                       "be answered from an invented prior",))
        return ContinuityBundle(
            referenced=True, resolved=True, kind=",".join(kinds),
            items=tuple(items[:8]),
            context_text="\n".join(context_parts)[:6000],
            clarifying_question=(
                "" if not missing else
                "I found: " + ", ".join(str(i.get("label", ""))[:60]
                                        for i in items[:3]) +
                ". I could not locate: " + ", ".join(missing) +
                " — should I include anything else?"),
            missing=tuple(missing), notes=tuple(notes))

    # -- per-kind resolution --------------------------------------------------------

    def _resolve_one(self, kind: str, session: Any, message: str
                     ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        now = self._now()
        if kind == "active_task" and session is not None:
            task_id = str(getattr(session, "active_task_id", "") or "")
            if task_id:
                out.append({"type": "task", "id": task_id,
                            "label": f"active task {task_id}",
                            "detail": self._task_detail(task_id)})
        if kind in ("previous", "compare", "discussion", "recent") and \
                session is not None:
            for ref in list(getattr(session, "research_refs", ()) or ())[:3]:
                out.append({"type": "research", "id": str(ref.get("id", "")),
                            "label": "research: " + str(ref.get("note", ""))[:80],
                            "at": float(ref.get("at", 0) or 0)})
            for ref in list(getattr(session, "artifacts", ()) or ())[:3]:
                out.append({"type": "artifact", "id": str(ref.get("id", "")),
                            "label": "artifact: " + str(ref.get("note", ""))[:80],
                            "at": float(ref.get("at", 0) or 0)})
        if kind in ("discussion", "yesterday", "deeper", "compare") and \
                self.memory is not None and hasattr(self.memory, "recall"):
            window = _WINDOW.get(kind)
            query = _recall_query(kind, message)
            hits = self._recall(query, window=window)
            for hit in hits:
                out.append(hit)
        if kind == "deeper" and session is not None:
            for turn in reversed(list(getattr(session, "history", ()) or ())):
                if str(turn.get("kind", "")) in ("research", "answer"):
                    out.append({"type": "last_answer",
                                "id": f"turn-{turn.get('seq')}",
                                "label": "previous answer (go deeper)",
                                "detail": str(turn.get("text", ""))[:600]})
                    break
        if kind == "checkpoint":
            # Checkpoints are owned by the run store; expose the pointer only.
            task_id = str(getattr(session, "active_task_id", "") or "") if \
                session is not None else ""
            if task_id:
                out.append({"type": "checkpoint", "id": task_id,
                            "label": f"checkpoint of task {task_id}",
                            "detail": "checkpoint listing is read via the "
                                      "task API (/api/v1/tasks/{id})"})
        # time-window filtering for "yesterday"
        if kind == "yesterday":
            window = _WINDOW["yesterday"]
            out = [item for item in out
                   if item.get("at") is None
                   or window[0] <= (now - float(item.get("at") or now)) <= window[1]
                   or item.get("type") in ("memory", "task")]
        if kind == "project" and session is not None:
            for ref in list(getattr(session, "memory_refs", ()) or ())[:4]:
                out.append({"type": "memory", "id": str(ref.get("id", "")),
                            "label": "memory: " + str(ref.get("note", ""))[:80]})
        # de-dup preserving order
        unique: List[Dict[str, Any]] = []
        seen: set = set()
        for item in out:
            key = (item.get("type"), item.get("id"), item.get("label"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _recall(self, query: str, *, window: Optional[Tuple[float, float]] = None
                ) -> List[Dict[str, Any]]:
        try:
            hits = list(self.memory.recall(query, k=5) or [])
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        now = self._now()
        for hit in hits:
            record = getattr(hit, "record", hit)
            created = float(getattr(record, "created_at", 0) or 0)
            if window is not None:
                age = now - created
                if not (window[0] <= age <= window[1]):
                    continue
            out.append({"type": "memory",
                        "id": str(getattr(record, "id", "")),
                        "label": str(getattr(record, "content", ""))[:120],
                        "detail": str(getattr(record, "content", ""))[:600],
                        "at": created})
        return out

    def _task_detail(self, task_id: str) -> str:
        if self.task_lookup is None or not task_id:
            return ""
        try:
            run = self.task_lookup(task_id) or {}
        except Exception:
            return ""
        if not isinstance(run, dict):
            return ""
        bits = [str(run.get("status", ""))[:24]]
        if run.get("requirement"):
            bits.append(str(run["requirement"])[:160])
        return " · ".join(b for b in bits if b)


def _recall_query(kind: str, message: str) -> str:
    if kind == "yesterday":
        return "yesterday session summary research decision"
    if kind == "discussion":
        return "conversation discussed summary"
    if kind == "compare":
        return "result comparison research finding"
    if kind == "deeper":
        return "answer research findings summary"
    return "recent summary"


def _render(item: Dict[str, Any]) -> str:
    label = str(item.get("label", ""))[:200]
    detail = str(item.get("detail", ""))[:600]
    if not label:
        return ""
    return f"[{item.get('type', 'ref')}] {label}" + (
        f" — {detail}" if detail else "")
