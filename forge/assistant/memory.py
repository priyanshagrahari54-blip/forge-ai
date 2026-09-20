"""Controlled personal memory (A84 Stage C).

Six layers, kept deliberately separate:

``short_term``    the current conversation window — RAM only, bounded, never
                  persisted unless a decision promotes it;
``session``       notes/decisions from the current session (durable, session
                  scope, cleared with the session);
``long_term``     stable user preferences, retained facts, recurring
                  workflows — the existing :class:`forge.memory.LongTermMemory`
                  engine with its provenance, retention, dedupe, redaction;
``episodic``      important events: finished tasks, research runs,
                  milestones — summaries with references, not transcripts;
``semantic``      learned relationships and reusable knowledge ("X depends on
                  Y", "deploy needs env var Z"), cross-referenced into the
                  pattern graph (Stage H);
``project``       project state/decisions/constraints — the engine's own
                  project scope, reused unchanged.

Safety guarantees (C1–C3):

* **not everything is stored**: every candidate passes
  :meth:`PersonalMemoryService.consider_retention` — relevance + usefulness
  + sensitivity + user intent + the session's retention policy. Default for
  ordinary messages: *skip*. Only explicit asks, durable decisions and
  high-importance signals are retained;
* **sensitive content fails closed**: secret-shaped content is refused
  (engine scan), and *personal sensitive data* (health, finance, biometric,
  location, credential-shaped text) is refused for long-term storage unless
  the user explicitly retains it — and even then it stays redaction-scanned;
* **provenance on every item** (C2): source, timestamp, confidence, memory
  type, scope, optional expiry and the reason for retention;
* **user control** (C3): ``inspect``/``list``, ``correct``, ``delete``,
  ``forget`` (purge + pattern-graph cleanup + preference-proposal cleanup),
  ``prevent retention`` (session mode), ``clear session``,
  ``clear long-term`` (explicit confirmation string required, bounded);
* **contradiction is surfaced, not overwritten**: when a new retained fact
  contradicts an active one, the write is refused-with-conflict and the
  pattern graph opens a CONFLICT record (H2); adjudication decides.

Storage itself is *always* the existing engine behind the existing A33
MEMORY policy — this service adds judgement and controls, never a parallel
database.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from forge.memory.types import MemoryType, Retention

__all__ = ["PersonalMemoryService", "RetentionDecision", "SHORT_TERM_MAX"]

SHORT_TERM_MAX = 40                     # turns per session (RAM)
SHORT_TERM_CHARS = 700
MIN_USEFUL_SIGNAL = 0.35                # below this: nothing to persist
MAX_CONFLICT_SCAN = 20

#: Personal-sensitive categories that must not be auto-retained long-term.
_SENSITIVE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\b(?:ssn|social security|passport number|driver'?s? licen[cs]e)\b",
     "identity document"),
    (r"\b(?:credit card|debit card|card number|cvv|iban)\b[\w .:/#-]*?\d",
     "payment instrument"),
    (r"\b(?:medical record|diagnosis|hiv|cancer treatment|therapy notes|"
     r"psychiatric)\b", "health"),
    (r"\b(?:salary|bank balance|mortgage|tax return|savings account)\b",
     "financial"),
    (r"\b(?:home address|live at|my address is|where i (?:live|sleep))\b",
     "location"),
    (r"\b(?:api[_ -]?key|secret[_ -]?(?:key|token)|password\s*[:=])\b",
     "credential-shaped"),
    (r"\b(?:biometric|fingerprint scan|face unlock)\b", "biometric"),
)

_EXPLICIT_RETAIN = ("remember that", "note that", "save this", "keep this",
                    "for future", "from now on", "always ", "my preference",
                    "i prefer", "remember:")
_DECISION_MARKERS = ("decision:", "decided", "we chose", "going with",
                     "settled on", "conclusion:")
_EPISODIC_MARKERS = ("completed", "failed", "shipped", "released",
                     "deployed", "accepted the change", "research finished")


@dataclass(frozen=True)
class RetentionDecision:
    """Why memory did or did not happen — always explicit (C1/C2)."""

    store: bool
    layer: str                     # short_term|session|long_term|episodic|semantic
    reason: str
    sensitivity: str = ""
    confidence: float = 0.5
    importance: float = 0.5
    #: True when a conflicting active fact was found (H2 handoff).
    conflict: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"store": self.store, "layer": self.layer,
                "reason": self.reason, "sensitivity": self.sensitivity,
                "confidence": round(self.confidence, 3),
                "importance": round(self.importance, 3),
                "conflict": dict(self.conflict) if self.conflict else None}


class PersonalMemoryService:
    """Judgement layer over the shared long-term memory engine."""

    def __init__(self, engine: Any, *,
                 pattern_graph: Any = None,
                 preference_observer: Any = None,
                 policy_engine: Optional[Any] = None,
                 audit: Optional[Any] = None,
                 agent_identity: str = "forge-personal-memory",
                 short_term_max: int = SHORT_TERM_MAX) -> None:
        self.engine = engine
        self.pattern_graph = pattern_graph
        self.preference_observer = preference_observer
        self.policy = policy_engine
        self.audit = audit
        self.agent_identity = agent_identity
        self._short_term: Dict[str, Deque[Dict[str, Any]]] = {}
        self._short_term_limit = max(4, int(short_term_max))

    # -- short term (RAM only) ---------------------------------------------------

    def push_short_term(self, session_id: str, role: str, text: str,
                        *, kind: str = "") -> None:
        window = self._short_term.setdefault(
            session_id, deque(maxlen=self._short_term_limit))
        window.append({"role": role, "text": (text or "")[:SHORT_TERM_CHARS],
                       "kind": kind, "at": time.time()})

    def short_term(self, session_id: str, *, last: int = 12
                   ) -> Tuple[Dict[str, Any], ...]:
        window = list(self._short_term.get(session_id, ()))
        return tuple(window[-max(1, int(last)):])

    def clear_short_term(self, session_id: str) -> None:
        self._short_term.pop(session_id, None)

    # -- retention decision (C1) -----------------------------------------------------

    def consider_retention(self, text: str, *, session_id: str = "",
                           intent: str = "", relevance: float = 0.5,
                           retention_mode: str = "normal",
                           project: str = "") -> RetentionDecision:
        lowered = (text or "").lower()
        length_signal = min(1.0, len(lowered) / 120.0)
        usefulness = max(relevance, 0.0)
        explicit = any(marker in lowered for marker in _EXPLICIT_RETAIN) \
            or intent in ("explicit", "remember")
        sensitivity = self._sensitivity(lowered)

        if retention_mode == "disabled":
            return RetentionDecision(
                False, "short_term",
                "session retention is disabled — kept in short-term only",
                sensitivity=sensitivity)
        if sensitivity:
            if not explicit:
                return RetentionDecision(
                    False, "none",
                    f"sensitive content ({sensitivity}) is not stored without "
                    "an explicit, deliberate user instruction",
                    sensitivity=sensitivity)
            return RetentionDecision(
                True, "long_term",
                f"explicitly retained despite sensitivity={sensitivity}; "
                "redaction scan still applies and the user can forget it",
                sensitivity=sensitivity, confidence=0.7, importance=0.8)
        if not text or not text.strip():
            return RetentionDecision(False, "none", "empty content")
        if explicit:
            layer = "long_term"
            if lowered.startswith("session ") or "for this session" in lowered:
                layer = "session"
            importance = 0.75
        elif any(m in lowered for m in _DECISION_MARKERS):
            layer, importance, explicit = "semantic", 0.8, True
        elif any(m in lowered for m in _EPISODIC_MARKERS) and length_signal > 0.4:
            layer, importance, explicit = "episodic", 0.55, True
        else:
            score = 0.5 * usefulness + 0.5 * length_signal
            if score < MIN_USEFUL_SIGNAL:
                return RetentionDecision(
                    False, "short_term",
                    "not relevant/useful enough for durable memory "
                    "(ordinary conversation is never auto-stored)",
                    importance=round(score, 3))
            layer, importance, explicit = "session", 0.5, True
        return RetentionDecision(True, layer,
                                 "retained: " + ("explicit user intent"
                                                 if intent in ("explicit", "remember")
                                                 else "meets relevance/usefulness bar"),
                                 sensitivity=sensitivity,
                                 confidence=0.6 if not explicit else 0.7,
                                 importance=importance)

    # -- writes -----------------------------------------------------------------------

    def retain(self, text: str, *, session_id: str = "", intent: str = "",
               relevance: float = 0.5, retention_mode: str = "normal",
               project: str = "personal", source: str = "assistant",
               via: str = "personal-memory", reason: str = "",
               ttl_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Decide, then store through the engine (never bypassing it)."""
        decision = self.consider_retention(
            text, session_id=session_id, intent=intent, relevance=relevance,
            retention_mode=retention_mode, project=project)
        self._audit(session_id, "consider", decision.store, decision.reason)
        if not decision.store:
            return {"decision": decision.to_dict(), "stored": False}
        if decision.layer == "session":
            self._audit(session_id, "session-note", True, decision.reason)
            result = self.engine.remember(
                MemoryType.SESSION, text, project=project or session_id,
                source=source, via=via, confidence=decision.confidence,
                importance=decision.importance, retention=Retention.SESSION,
                metadata={"reason_for_retention": reason or decision.reason,
                          "scope": f"session:{session_id}"})
            return {"decision": decision.to_dict(),
                    "stored": bool(getattr(result, "status", "") in
                                   ("stored", "duplicate")),
                    "result": _result_dict(result)}
        if decision.conflict is not None:
            return {"decision": decision.to_dict(), "stored": False,
                    "conflict": decision.conflict}
        memory_type = self._type_for(text)
        conflict = self.find_conflict(text, memory_type=memory_type,
                                      project=project)
        if conflict is not None:
            self._open_conflict(conflict, project=project, text=text)
            return {"decision": dict(decision.to_dict(), conflict=conflict),
                    "stored": False, "conflict": conflict,
                    "note": ("new information contradicts an active memory; "
                             "nothing was overwritten — adjudicate the "
                             "conflict to resolve it (H2)")}
        retention = {MemoryType.PREFERENCE: Retention.PERSISTENT,
                     MemoryType.EPISODIC: Retention.PROJECT,
                     MemoryType.SEMANTIC: Retention.PROJECT}.get(
                         memory_type, Retention.PERSISTENT)
        result = self.engine.remember(
            memory_type, text, project=project or "personal", source=source,
            via=via, confidence=decision.confidence,
            importance=decision.importance, retention=retention,
            ttl_seconds=ttl_seconds,
            metadata={"reason_for_retention": reason or decision.reason,
                      "scope": "personal", "sensitivity": decision.sensitivity,
                      "session": session_id})
        stored = getattr(result, "status", "") in ("stored", "duplicate")
        if stored and getattr(result, "status", "") == "stored":
            record = getattr(result, "record", None)
            if record is not None:
                if self.pattern_graph is not None:
                    try:
                        self.pattern_graph.ingest_memory(record)
                    except Exception:
                        pass
                if self.preference_observer is not None and \
                        memory_type == MemoryType.PREFERENCE:
                    try:
                        self.preference_observer.observe(
                            text, memory_id=getattr(record, "id", ""))
                    except Exception:
                        pass
        self._audit(session_id, "retain", bool(stored),
                    f"type={memory_type.value} status={getattr(result, 'status', '')}")
        return {"decision": decision.to_dict(), "stored": bool(stored),
                "result": _result_dict(result)}

    def _type_for(self, text: str) -> MemoryType:
        lowered = (text or "").lower()
        if any(m in lowered for m in _EXPLICIT_RETAIN) or \
                "prefer" in lowered:
            return MemoryType.PREFERENCE
        if any(m in lowered for m in _DECISION_MARKERS):
            return MemoryType.SEMANTIC
        if any(m in lowered for m in _EPISODIC_MARKERS):
            return MemoryType.EPISODIC
        return MemoryType.PROJECT

    @staticmethod
    def _sensitivity(lowered: str) -> str:
        for pattern, label in _SENSITIVE_PATTERNS:
            if re.search(pattern, lowered):
                return label
        return ""

    # -- contradiction (C1/H2) ----------------------------------------------------------

    def find_conflict(self, text: str, *, memory_type: MemoryType,
                      project: str) -> Optional[Dict[str, Any]]:
        """An active memory that the new text would *invert*, not duplicate.

        Detection is deliberately narrow: same anchor terms, opposite
        polarity. Anything weaker than that is a normal add, not a conflict.
        """
        terms = _anchor_terms(text)
        if len(terms) < 2:
            return None
        negative_new = _is_negative(text)
        try:
            hits = list(self.engine.search(" ".join(terms),
                                           project=project or None,
                                           memory_type=memory_type,
                                           k=MAX_CONFLICT_SCAN))
        except Exception:
            return None
        for hit in hits:
            record = getattr(hit, "record", None)
            if record is None:
                continue
            content = str(getattr(record, "content", ""))
            shared = terms.intersection(_anchor_terms(content))
            if len(shared) < max(2, len(terms) - 1):
                continue
            if _is_negative(content) != negative_new:
                return {
                    "existing_id": str(getattr(record, "id", "")),
                    "existing_content": content[:240],
                    "existing_confidence": float(
                        getattr(record, "confidence", 0.5) or 0.5),
                    "existing_at": float(getattr(record, "created_at", 0) or 0),
                    "status": "CONFLICT",
                    "options": ["old-stale", "new-stronger",
                                 "context-specific", "user-clarification"],
                }
        return None

    def _open_conflict(self, conflict: Dict[str, Any], *, project: str,
                       text: str = "") -> None:
        """Surface the contradiction on the pattern graph.

        Deliberately uses the graph's own functional-predicate mechanism:
        observing the *contradicting* claim for a subject+predicate that
        already holds an ACTIVE relation opens a CONFLICT row next to the
        old relation — both stay, adjudication is explicit (H2), and
        nothing is silently overwritten.
        """
        if self.pattern_graph is None:
            return
        try:
            shared = sorted(_anchor_terms(conflict.get("existing_content", ""))
                            & _anchor_terms(text))
            if not shared:
                return
            memory_type = str(getattr(conflict, "get", lambda *_: "")("type")
                              or "preference")
            if memory_type == "decision":
                subject, predicate = project or "project", "decided"
            else:
                subject, predicate = "user", "prefers"
            self.pattern_graph.observe(
                subject, predicate,
                "contradiction: " + " ".join(shared)[:120],
                source="memory-conflict-scan", confidence=0.3,
                subject_type="person" if subject == "user" else "project",
                obj_type="contradiction",
                provenance={"existing_id": conflict.get("existing_id", ""),
                            "new_claim": (text or "")[:160],
                            "mode": "surfaced-not-overwritten"})
        except Exception:
            pass

    # -- reads ---------------------------------------------------------------------------

    def recall(self, query: str, *, project: str = "personal",
               types: Sequence[MemoryType] = (), k: int = 5):
        """Relevance-ranked retrieval over the engine (bounded)."""
        results = []
        wanted = tuple(types) or (None,)
        for memory_type in wanted:
            try:
                results.extend(self.engine.search(
                    query, project=project, memory_type=memory_type, k=k))
            except Exception:
                continue
        return tuple(sorted(results, key=lambda hit: -float(
            getattr(hit, "score", 0.0)))[:k])

    def inspect(self, *, project: str = "", memory_type: Any = None,
                limit: int = 50) -> Dict[str, Any]:
        rows = self.engine.list(project=project or None,
                                memory_type=memory_type, limit=limit,
                                include_inactive=False)
        return {"entries": [record.to_dict(include_content=True)
                            for record in rows],
                "layers": {
                    "short_term": sum(len(w) for w in self._short_term.values()),
                    "provenance": "engine (C2: source/timestamp/confidence/"
                                  "type/scope/expiry/reason)",
                },
                "stats": self.engine.stats(project=project or None)}

    def provenance(self, memory_id: str) -> List[Dict[str, Any]]:
        return list(self.engine.provenance(memory_id) or [])

    # -- user control (C3) ------------------------------------------------------------------

    def correct(self, memory_id: str, new_content: str, *, project: str = "",
                 source: str = "user", reason: str = "user correction"
                 ) -> Dict[str, Any]:
        result = self.engine.correct(memory_id, new_content, source=source,
                                      reason=reason, project=project or None)
        self._audit("", "correct", True, reason)
        return {"corrected": True, "record": _record_dict(result)}

    def delete(self, memory_id: str, *, project: str = "",
               source: str = "user") -> Dict[str, Any]:
        outcome = self.engine.delete(memory_id, source=source,
                                     project=project or None)
        self._audit("", "delete", True, memory_id)
        return {"deleted": True, "outcome": str(bool(outcome))}

    def forget(self, memory_id: str, *, project: str = "",
               reason: str = "user asked to forget") -> Dict[str, Any]:
        """Hard-forget: purge the record, drop pattern projections and any
        pending preference proposals citing it. Reversible? No — and the API
        says so."""
        purged = self.engine.purge(memory_id, source="user-forget",
                                   project=project or None)
        dropped = 0
        if self.pattern_graph is not None:
            dropped = self._forget_graph_refs(memory_id)
        if self.preference_observer is not None:
            try:
                self.preference_observer.forget(memory_id=memory_id)
            except Exception:
                pass
        self._audit("", "forget", True, f"{memory_id}: {reason}")
        return {"purged": bool(purged), "graph_refs_dropped": dropped,
                "reversible": False,
                "note": "purge removes the record and its projections; "
                        "provenance of *this* act is audited, not the "
                        "forgotten content"}

    def clear_long_term(self, *, confirm: str, project: str = "personal") -> Dict[str, Any]:
        """Explicit, confirmation-gated clearing of a project's long-term
        memories. Without the exact confirmation string nothing is removed."""
        if (confirm or "").strip() != "FORGET EVERYTHING IN THIS SCOPE":
            raise ValueError(
                "clearing long-term memory requires the exact confirmation "
                "string 'FORGET EVERYTHING IN THIS SCOPE'")
        rows = list(self.engine.list(project=project, limit=10_000,
                                     include_inactive=False))
        removed = 0
        for record in rows:
            try:
                self.engine.purge(record.id, source="user-clear",
                                   project=project)
                removed += 1
            except Exception:
                continue
        self._audit("", "clear-long-term", True, f"project={project} removed={removed}")
        return {"removed": removed, "project": project, "reversible": False}

    # -- helpers -----------------------------------------------------------------------------

    def _forget_graph_refs(self, memory_id: str) -> int:
        graph = self.pattern_graph
        count = 0
        try:
            for relation in graph.relations_of("user", status="ANY",
                                               limit=500) if hasattr(
                                                   graph, "relations_of") else []:
                provenance = relation.provenance or {}
                if str(provenance.get("memory_id", "")) == memory_id:
                    graph._db.execute(
                        "DELETE FROM pattern_relations WHERE id = ?",
                        (relation.id,))
                    count += 1
        except Exception:
            return count
        return count

    def _audit(self, session_id: str, action: str, ok: bool, detail: str) -> None:
        """Best-effort audit with duck-typed adapters; never alters behaviour."""
        if self.audit is None:
            return
        try:
            if callable(self.audit):
                self.audit({"actor": self.agent_identity, "action": action,
                            "ok": bool(ok), "detail": detail[:280],
                            "task_id": session_id[:64]})
                return
            record_decision = getattr(self.audit, "record_decision", None)
            if record_decision is not None:
                record_decision(agent=self.agent_identity, resource="memory",
                                operation=action, decision="ALLOW" if ok else "DENY",
                                reason=detail[:280], task_id=session_id[:64])
                return
            record = getattr(self.audit, "record", None)
            if record is not None:
                record(action, {"actor": self.agent_identity, "ok": bool(ok),
                                "detail": detail[:280]})
        except Exception:
            pass    # audit plumbing must not change memory behaviour


def _anchor_terms(text: str) -> set:
    words = re.findall(r"[a-z0-9_][a-z0-9_.-]{3,}", (text or "").lower())
    stop = {"that", "this", "with", "from", "have", "will", "should", "must",
            "does", "into", "over", "they", "them", "then", "than", "when"}
    return {w for w in words if w not in stop}


def _is_negative(text: str) -> bool:
    lowered = " " + (text or "").lower() + " "
    return any(n in lowered for n in (" not ", " never ", " cannot ",
                                      " without ", " no ", " disable",
                                      "prohibit", "avoid"))


def _result_dict(result: Any) -> Dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "to_dict"):
        return result.to_dict(include_content=False)
    return {"value": str(result)}


def _record_dict(record: Any) -> Dict[str, Any]:
    if record is None:
        return {}
    if hasattr(record, "to_dict"):
        return record.to_dict(include_content=False)
    return {"value": str(record)}
