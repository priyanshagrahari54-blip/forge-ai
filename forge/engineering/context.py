"""Shared project context (A83): agents stop rediscovering the repository.

Before A83 every agent request rebuilt its own view of the project from
:class:`~forge.intelligence.repository.RepositoryIntelligence`. That is correct
but wasteful, and worse: nothing accumulated. Each agent started from zero.

:class:`ProjectContext` is the single structured object a team of agents shares.
It holds the plan, the classified requirement, the active profiles, the
repository index summary, the results of engines that already ran, and the
decisions taken so far. It is versioned and fingerprinted, so an agent can tell
whether the context it is holding is still current.

Rules:

* Facts are recorded with their **source** (``build``, ``tests``, ``probe``,
  ``agent:<name>``, ``user``). A fact with no source is refused.
* Recording is append-only for history and last-write-wins for facts, so a
  stale measurement can never silently overwrite a newer one.
* Nothing here runs anything; it is the shared memory of the team.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_FACTS = 2_000
MAX_HISTORY = 500
MAX_VALUE_BYTES = 256 * 1024
#: Kinds of fact that may be recorded. Free-form kinds are allowed but the
#: canonical ones are what the engines and the cockpit understand.
FACT_KINDS = ("requirement", "plan", "architecture", "decision", "constraint",
              "build", "tests", "benchmark", "diagnostic", "hardware",
              "boot", "release", "observation", "risk")


class ContextError(ValueError):
    """Raised for an invalid context operation."""


@dataclass(frozen=True)
class Fact:
    """One recorded piece of project knowledge."""

    key: str
    kind: str
    value: Any
    source: str
    at: float = field(default_factory=time.time)
    #: Set when the fact is a measurement that could go stale.
    confidence: float = 1.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "value": self.value,
                "source": self.source, "at": self.at,
                "confidence": round(self.confidence, 3), "note": self.note}


@dataclass
class AgentTurn:
    """One agent's contribution, recorded so the team can see who did what."""

    agent: str
    role: str
    at: float
    summary: str
    ok: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"agent": self.agent, "role": self.role, "at": self.at,
                "summary": self.summary, "ok": self.ok,
                "detail": dict(self.detail)}


class ProjectContext:
    """The structured context a team of agents shares for one project."""

    def __init__(self, project: str, *, root: Optional[Path] = None) -> None:
        self.project = project
        self.root = Path(root).resolve() if root else None
        self.facts: Dict[str, Fact] = {}
        self.history: List[AgentTurn] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.revision = 0

    # -- recording -------------------------------------------------------

    def record(self, key: str, kind: str, value: Any, *, source: str,
               confidence: float = 1.0, note: str = "") -> Fact:
        """Record (or replace) one fact. Requires a source."""
        if not key or not str(key).strip():
            raise ContextError("a fact needs a key")
        if not source or not str(source).strip():
            raise ContextError(
                "a fact needs a source: an unsourced fact cannot be trusted")
        kind = str(kind or "observation").strip()
        if kind not in FACT_KINDS:
            # The vocabulary is the point: an untyped fact cannot be queried
            # by kind later, which is what makes this a context rather than a
            # pile of notes.
            raise ContextError(
                "unknown fact kind %r; expected one of %s"
                % (kind, ", ".join(FACT_KINDS)))
        try:
            encoded = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise ContextError("fact value is not serialisable: %s" % exc)
        if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ContextError(
                "fact %r exceeds the %d-byte bound" % (key, MAX_VALUE_BYTES))
        previous = self.facts.get(key)
        fact = Fact(key=str(key).strip(), kind=kind,
                    value=value, source=str(source).strip(),
                    confidence=max(0.0, min(1.0, float(confidence))),
                    note=str(note or ""))
        if previous is not None and previous.at > fact.at:
            # A late-arriving measurement must not overwrite a newer one.
            return previous
        self.facts[fact.key] = fact
        self._bump()
        if len(self.facts) > MAX_FACTS:
            for stale in sorted(self.facts, key=lambda item: self.facts[item].at):
                del self.facts[stale]
                if len(self.facts) <= MAX_FACTS:
                    break
        return fact

    def record_many(self, items: Sequence[Dict[str, Any]], *,
                    source: str) -> int:
        count = 0
        for item in items:
            if not isinstance(item, dict) or "key" not in item:
                continue
            self.record(str(item["key"]), str(item.get("kind", "observation")),
                        item.get("value"), source=source,
                        confidence=float(item.get("confidence", 1.0) or 1.0),
                        note=str(item.get("note", "")))
            count += 1
        return count

    def note_turn(self, agent: str, role: str, summary: str, *,
                  ok: bool = True, **detail: Any) -> AgentTurn:
        """Record that an agent ran and what it concluded."""
        turn = AgentTurn(agent=str(agent), role=str(role), at=time.time(),
                         summary=str(summary)[:4_000], ok=bool(ok),
                         detail={key: detail[key] for key in sorted(detail)})
        self.history.append(turn)
        self.history = self.history[-MAX_HISTORY:]
        self._bump()
        return turn

    def _bump(self) -> None:
        self.revision += 1
        self.updated_at = time.time()

    # -- reading ---------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        fact = self.facts.get(key)
        return default if fact is None else fact.value

    def fact(self, key: str) -> Optional[Fact]:
        return self.facts.get(key)

    def by_kind(self, kind: str) -> List[Fact]:
        return sorted((fact for fact in self.facts.values()
                       if fact.kind == kind), key=lambda item: item.key)

    def turns(self, agent: str = "") -> List[AgentTurn]:
        if not agent:
            return list(self.history)
        return [turn for turn in self.history if turn.agent == agent]

    def agents_involved(self) -> List[str]:
        return sorted({turn.agent for turn in self.history})

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"facts": {key: self.facts[key].to_dict()
                       for key in sorted(self.facts)},
             "revision": self.revision}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "root": str(self.root) if self.root else "",
            "revision": self.revision,
            "fingerprint": self.fingerprint(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "facts": {key: self.facts[key].to_dict()
                      for key in sorted(self.facts)},
            "history": [turn.to_dict() for turn in self.history],
            "agents": self.agents_involved(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProjectContext":
        if not isinstance(payload, dict):
            raise ContextError("a project context must be an object")
        context = cls(str(payload.get("project", "")),
                      root=Path(payload["root"]) if payload.get("root") else None)
        context.revision = int(payload.get("revision", 0) or 0)
        context.created_at = float(payload.get("created_at", time.time()))
        context.updated_at = float(payload.get("updated_at", time.time()))
        for key, item in (payload.get("facts") or {}).items():
            if not isinstance(item, dict):
                continue
            context.facts[str(key)] = Fact(
                key=str(item.get("key", key)),
                kind=str(item.get("kind", "observation")),
                value=item.get("value"),
                source=str(item.get("source", "")),
                at=float(item.get("at", 0.0) or 0.0),
                confidence=float(item.get("confidence", 1.0) or 1.0),
                note=str(item.get("note", "")))
        for item in payload.get("history") or []:
            if not isinstance(item, dict):
                continue
            context.history.append(AgentTurn(
                agent=str(item.get("agent", "")),
                role=str(item.get("role", "")),
                at=float(item.get("at", 0.0) or 0.0),
                summary=str(item.get("summary", "")),
                ok=bool(item.get("ok", True)),
                detail=dict(item.get("detail") or {})))
        return context

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True,
                                     default=str), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ProjectContext":
        target = Path(path)
        if not target.is_file():
            raise ContextError("no such context file: %s" % target)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContextError("context file is unreadable: %s" % exc) from exc
        return cls.from_dict(payload)

    # -- derived views ---------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """A compact view of what is currently known and how it was learned."""
        kinds: Dict[str, int] = {}
        sources: Dict[str, int] = {}
        for fact in self.facts.values():
            kinds[fact.kind] = kinds.get(fact.kind, 0) + 1
            sources[fact.source] = sources.get(fact.source, 0) + 1
        return {
            "project": self.project,
            "revision": self.revision,
            "fingerprint": self.fingerprint(),
            "facts": len(self.facts),
            "by_kind": {key: kinds[key] for key in sorted(kinds)},
            "by_source": {key: sources[key] for key in sorted(sources)},
            "agents": self.agents_involved(),
            "turns": len(self.history),
        }
