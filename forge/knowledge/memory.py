"""Long-term project memory (A83).

A repository needs to remember what was decided and what was tried, in a form
that can be searched and acted on later. Conversation transcripts are not that
form: they are ordered, verbose, and contain a great many statements that were
later withdrawn.

This store keeps *kinds* of durable fact:

=====================  ======================================================
``decision``           an architecture decision, with the alternatives and the
                       reason it was chosen
``requirement``        something the project must do
``constraint``         something that may not be done
``design``             a design choice below the level of a decision
``bug``                a known defect
``failed_approach``    something tried that did not work, and why — the kind
                       of memory that prevents the same week being spent twice
``solution``           an approach that worked, and where it applies
``benchmark``          a measured number, with its before/after context
``dependency``         a required package, tool, or service
``api``                a published interface
``convention``         a rule the project follows
``testing``            what must be tested and how
=====================  ======================================================

Entries are content-addressed by id, supersede their predecessors explicitly,
and every write records who made it and when. Nothing here is a cache: it is
the project's record of itself.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DECISION = "decision"
REQUIREMENT = "requirement"
CONSTRAINT = "constraint"
DESIGN = "design"
BUG = "bug"
FAILED_APPROACH = "failed-approach"
SOLUTION = "solution"
BENCHMARK = "benchmark"
DEPENDENCY = "dependency"
API = "api"
CONVENTION = "convention"
TESTING = "testing"

KINDS = (DECISION, REQUIREMENT, CONSTRAINT, DESIGN, BUG, FAILED_APPROACH,
         SOLUTION, BENCHMARK, DEPENDENCY, API, CONVENTION, TESTING)

#: Kinds that describe what must be true, and so should be re-checked.
MUST_HOLD = (REQUIREMENT, CONSTRAINT, TESTING)

OPEN = "open"
RESOLVED = "resolved"
SUPERSEDED = "superseded"
STATUSES = (OPEN, RESOLVED, SUPERSEDED)

DEFAULT_PATH = Path(".forge/memory.json")
MAX_ENTRIES = 4_000


class MemoryError(Exception):
    """Raised when a memory write is incomplete or contradictory."""


@dataclass
class MemoryEntry:
    """One durable fact about the project."""

    kind: str
    title: str
    detail: str = ""
    #: Where this fact applies, as repository-relative paths or component ids.
    scope: List[str] = field(default_factory=list)
    #: Free-form tags, e.g. the ADR id or the subsystem.
    tags: List[str] = field(default_factory=list)
    status: str = OPEN
    confidence: float = 1.0
    #: Id of the entry this one replaces, when it replaces one.
    supersedes: str = ""
    #: Who asserted it: an agent, a person, or a tool run.
    source: str = ""
    #: Extra structured payload — a benchmark's numbers, a dependency's
    #: version. Must be JSON-serialisable.
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.kind = str(self.kind or "").strip()
        if self.kind not in KINDS:
            raise MemoryError(
                "unknown memory kind %r; expected one of %s"
                % (self.kind, ", ".join(KINDS)))
        self.title = str(self.title or "").strip()
        if not self.title:
            raise MemoryError("a memory entry needs a title")
        if self.status not in STATUSES:
            raise MemoryError("unknown memory status %r" % self.status)
        self.scope = [str(item) for item in self.scope if str(item).strip()]
        self.tags = [str(item).strip().lower() for item in self.tags
                     if str(item).strip()]
        self.source = str(self.source or "").strip()
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        if self.kind == BENCHMARK and "metrics" not in self.payload:
            # A benchmark without numbers is an opinion, and opinions do not
            # belong in this store under that kind.
            raise MemoryError(
                "a benchmark entry needs payload['metrics']; an unmeasured "
                "claim is not a benchmark")
        if not self.id:
            self.id = self.compute_id()
        now = time.time()
        self.created_at = float(self.created_at or now)
        self.updated_at = float(self.updated_at or now)

    def compute_id(self) -> str:
        material = json.dumps(
            {"kind": self.kind, "title": self.title,
             "scope": sorted(self.scope)}, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def text(self) -> str:
        return " ".join(filter(None, (self.title, self.detail,
                                      " ".join(self.tags))))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MemoryEntry":
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items()
                      if key in allowed})


class ProjectMemory:
    """Bounded, searchable long-term memory for one repository."""

    def __init__(self, root: str | Path = ".",
                 path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / Path(path if path is not None
                                     else DEFAULT_PATH)
        self._entries: Dict[str, MemoryEntry] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()
        self._loaded = False
        self.load_error = ""

    # -- persistence -----------------------------------------------------

    def load(self) -> "ProjectMemory":
        with self._lock:
            if self._loaded:
                return self
            self._loaded = True
            self.load_error = ""
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return self
            except (ValueError, OSError) as exc:
                self.load_error = "%s: %s" % (type(exc).__name__, exc)
                return self
            items = payload.get("entries", []) if isinstance(payload, dict) \
                else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    entry = MemoryEntry.from_dict(item)
                except MemoryError:
                    continue
                self._entries[entry.id] = entry
                self._order.append(entry.id)
            return self

    def save(self) -> Path:
        with self._lock:
            self.load()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = [self._entries[item].to_dict()
                       for item in self._order if item in self._entries]
            payload = {"version": 1, "entries": entries}
            self.path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
            return self.path

    # -- writing ---------------------------------------------------------

    def record(self, entry: MemoryEntry, *, save: bool = True) -> MemoryEntry:
        """Add or update an entry, marking anything it supersedes."""
        with self._lock:
            self.load()
            if entry.supersedes:
                previous = self._entries.get(entry.supersedes)
                if previous is None:
                    raise MemoryError(
                        "cannot supersede unknown entry %r"
                        % entry.supersedes)
                if previous.status == OPEN:
                    previous.status = SUPERSEDED
                    previous.updated_at = time.time()
            self._entries[entry.id] = entry
            if entry.id not in self._order:
                self._order.append(entry.id)
            self._prune()
            if save:
                self.save()
            return entry

    def add(self, kind: str, title: str, detail: str = "", *,
            scope: Sequence[str] = (), tags: Sequence[str] = (),
            source: str = "", status: str = OPEN, confidence: float = 1.0,
            payload: Optional[Dict[str, Any]] = None,
            supersedes: str = "", save: bool = True) -> MemoryEntry:
        """Convenience wrapper around :meth:`record`."""
        if not source:
            raise MemoryError(
                "a memory entry needs a source; an unattributed fact cannot "
                "be checked later")
        entry = MemoryEntry(
            kind=kind, title=title, detail=detail, scope=list(scope),
            tags=list(tags), status=status, confidence=confidence,
            payload=dict(payload or {}), source=source,
            supersedes=supersedes)
        return self.record(entry, save=save)

    def resolve(self, entry_id: str, *, note: str = "",
                save: bool = True) -> MemoryEntry:
        """Mark an entry resolved (a bug fixed, a requirement met)."""
        with self._lock:
            self.load()
            entry = self._entries.get(entry_id)
            if entry is None:
                raise MemoryError("unknown entry %r" % entry_id)
            entry.status = RESOLVED
            entry.updated_at = time.time()
            if note:
                entry.payload["resolution"] = note
            if save:
                self.save()
            return entry

    def _prune(self) -> None:
        if len(self._order) <= MAX_ENTRIES:
            return
        # Resolved facts age out before open ones: what still needs doing is
        # worth more than what was finished long ago.
        rank = {OPEN: 0, SUPERSEDED: 1, RESOLVED: 2}
        keep = sorted(
            self._order,
            key=lambda item: (rank.get(self._entries[item].status, 0),
                              -self._entries[item].updated_at))[:MAX_ENTRIES]
        keep_set = set(keep)
        dropped = [item for item in self._order if item not in keep_set]
        for item in dropped:
            self._entries.pop(item, None)
        self._order = keep

    # -- reading ---------------------------------------------------------

    def all(self) -> List[MemoryEntry]:
        with self._lock:
            self.load()
            return [self._entries[item] for item in self._order
                    if item in self._entries]

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        with self._lock:
            self.load()
            return self._entries.get(entry_id)

    def by_kind(self, kind: str, *, include_superseded: bool = False
                ) -> List[MemoryEntry]:
        return [entry for entry in self.all() if entry.kind == kind
                and (include_superseded or entry.status != SUPERSEDED)]

    def for_path(self, path: str, *, include_superseded: bool = False
                 ) -> List[MemoryEntry]:
        """Entries whose scope covers a path, by prefix or exact match."""
        needle = str(path).replace("\\", "/")
        found = []
        for entry in self.all():
            if not include_superseded and entry.status == SUPERSEDED:
                continue
            for item in entry.scope:
                item = item.replace("\\", "/")
                if item == needle or needle.startswith(item.rstrip("/") + "/"):
                    found.append(entry)
                    break
        return found

    def open_items(self, kinds: Sequence[str] = ()) -> List[MemoryEntry]:
        wanted = set(kinds or MUST_HOLD)
        return [entry for entry in self.all()
                if entry.status == OPEN and entry.kind in wanted]

    def search(self, query: str, *, limit: int = 20,
               kinds: Sequence[str] = ()) -> List[Tuple[MemoryEntry, float]]:
        """Exact-substring search with a small relevance score.

        Scoring is deliberately simple and explainable: a title hit outweighs a
        tag hit, which outweighs a detail hit, and confidence scales the total.
        There is no embedding model here and no claim of semantic search —
        :class:`~forge.intelligence.semantic.SemanticIndexer` is the semantic
        layer, and it indexes documents, not memory.
        """
        needle = str(query or "").strip().lower()
        if not needle:
            return []
        tokens = [item for item in needle.split() if item]
        if not tokens:
            return []
        results: List[Tuple[MemoryEntry, float]] = []
        for entry in self.all():
            if kinds and entry.kind not in kinds:
                continue
            if entry.status == SUPERSEDED:
                continue
            score = 0.0
            title = entry.title.lower()
            tags = " ".join(entry.tags)
            detail = entry.detail.lower()
            for token in tokens:
                if token in title:
                    score += 3.0
                if token in tags:
                    score += 2.0
                if token in detail:
                    score += 1.0
            if score <= 0:
                continue
            results.append((entry, round(score * (0.5 + entry.confidence / 2),
                                         4)))
        results.sort(key=lambda item: (-item[1], item[0].title))
        return results[:max(0, int(limit))]

    # -- reporting -------------------------------------------------------

    def counts(self) -> Dict[str, Any]:
        entries = self.all()
        by_kind: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        for entry in entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
        return {"entries": len(entries), "by_kind": by_kind,
                "by_status": by_status, "load_error": self.load_error}

    def summary(self) -> Dict[str, Any]:
        entries = self.all()
        return {
            **self.counts(),
            "open_bugs": len(self.by_kind(BUG)),
            "failed_approaches": len(self.by_kind(FAILED_APPROACH)),
            "decisions": len(self.by_kind(DECISION)),
            "requirements": len(self.by_kind(REQUIREMENT)),
            "constraints": len(self.by_kind(CONSTRAINT)),
            "benchmarks": len(self.by_kind(BENCHMARK)),
            "path": str(self.path.relative_to(self.root)),
        }


def render(memory: ProjectMemory, *, limit: int = 25) -> str:
    """A readable digest for the CLI and for agent prompts."""
    summary = memory.summary()
    lines = [
        "entries: %(entries)d  decisions: %(decisions)d  requirements: "
        "%(requirements)d  constraints: %(constraints)d  open bugs: "
        "%(open_bugs)d  failed approaches: %(failed_approaches)d" % summary,
    ]
    if summary.get("load_error"):
        lines.append("load error: %s" % summary["load_error"])
    shown = 0
    for kind in (DECISION, CONSTRAINT, REQUIREMENT, BUG, FAILED_APPROACH,
                 SOLUTION, BENCHMARK):
        entries = memory.by_kind(kind)
        if not entries:
            continue
        lines.append("")
        lines.append("%s (%d)" % (kind, len(entries)))
        for entry in entries[:limit]:
            marker = " [resolved]" if entry.status == RESOLVED else ""
            scope = " (%s)" % ", ".join(entry.scope[:3]) if entry.scope else ""
            lines.append("  - %s%s%s" % (entry.title, scope, marker))
            if entry.detail:
                lines.append("      %s" % entry.detail[:200])
            shown += 1
        if shown >= limit:
            break
    return "\n".join(lines)


def memory_prompt(memory: ProjectMemory, *, path: str = "",
                  limit: int = 12) -> str:
    """The slice of memory worth putting in front of an agent."""
    lines: List[str] = []
    relevant = memory.for_path(path) if path else []
    if relevant:
        lines.append("Relevant to %s:" % path)
        for entry in relevant[:limit]:
            lines.append("- [%s] %s" % (entry.kind, entry.title))
            if entry.detail:
                lines.append("  %s" % entry.detail[:200])
    constraints = memory.by_kind(CONSTRAINT)
    if constraints:
        lines.append("")
        lines.append("Constraints (these must not be violated):")
        for entry in constraints[:limit]:
            lines.append("- %s" % entry.title)
    failed = memory.by_kind(FAILED_APPROACH)
    if failed:
        lines.append("")
        lines.append("Already tried and did not work:")
        for entry in failed[:limit]:
            lines.append("- %s%s" % (entry.title,
                                     ": %s" % entry.detail[:160]
                                     if entry.detail else ""))
    bugs = memory.by_kind(BUG)
    if bugs:
        lines.append("")
        lines.append("Known open bugs:")
        for entry in bugs[:limit]:
            lines.append("- %s" % entry.title)
    return "\n".join(lines)
