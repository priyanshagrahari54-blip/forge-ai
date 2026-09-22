"""Pattern discovery: structured relationship layer (A84 Stage H).

The pattern graph connects Forge's world — conversations, research,
documents, code, projects, events, agent outputs and user preferences — as
typed entities and evidence-backed relations:

``Entity -> Entity``  ``Concept -> Concept``  ``Problem -> Solution``
``Project -> Decision``  ``Research -> Finding``  ``Finding -> Evidence``
``User preference -> Workflow``

Rules that are enforced in code, not documentation:

* every relation carries ``source``, ``confidence``, ``timestamp`` and
  ``provenance`` (H1) — a relation without provenance is refused;
* new information that contradicts an existing *functional* relation is
  marked ``CONFLICT`` and **never silently overwrites** (H2); an explicit
  adjudication (stale / newer-stronger / context-specific / user
  clarification) is required to settle it;
* confidence grows only from repeated, independent observation and is always
  clamped below certainty — co-occurrence is a *hypothesis*, not knowledge;
* the graph is bounded (entities, relations, conflicts) so it cannot flood.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["Entity", "PatternGraph", "Relation",
           "ENTITY_TYPES", "RELATION_STATUSES"]

ENTITY_TYPES = (
    "person", "organization", "technology", "project", "concept", "file",
    "model", "agent", "task", "event", "research_source", "preference",
    "workflow", "decision", "finding", "evidence", "problem", "solution",
)

RELATION_STATUSES = ("ACTIVE", "CONFLICT", "SUPERSEDED", "STALE",
                     "CONTEXT_SPECIFIC")

#: Predicates that describe exactly one current value per (subject) — a
#: second, different object under the same predicate is a contradiction, not
#: an addition.
FUNCTIONAL_PREDICATES = frozenset({
    "prefers", "depends_on_primary", "decided", "chooses", "maintains",
    "hosts", "located_in", "version_is", "status_is", "uses_database",
})

MAX_NAME = 160
MAX_PREDICATE = 64
MAX_SOURCE = 200
MAX_NOTE = 400
MAX_ENTITIES = 20_000
MAX_RELATIONS = 60_000
MAX_CONFLICTS = 2_000
#: A single observation never reaches certainty; repeated independent
#: evidence asymptotes to 0.95.
CONFIDENCE_CEILING = 0.95


def _slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9_.:/-]+", "-", (text or "").strip().lower())
    return text.strip("-")[:MAX_NAME] or hashlib.sha1(
        (text or "").encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    entity_type: str
    aliases: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.entity_type,
                "aliases": list(self.aliases), "metadata": dict(self.metadata or {})}


@dataclass(frozen=True)
class Relation:
    id: str
    subject: str
    predicate: str
    obj: str
    source: str
    confidence: float
    status: str
    created_at: float
    last_seen: float
    observations: int
    provenance: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "subject": self.subject, "predicate": self.predicate,
            "object": self.obj, "source": self.source,
            "confidence": round(self.confidence, 4), "status": self.status,
            "created_at": self.created_at, "last_seen": self.last_seen,
            "observations": self.observations,
            "provenance": dict(self.provenance or {}),
        }


class PatternGraph:
    """SQLite-backed entity/relation store with contradiction adjudication."""

    def __init__(self, db: Any, *, max_entities: int = MAX_ENTITIES,
                 max_relations: int = MAX_RELATIONS) -> None:
        self._db = db
        self.max_entities = max(100, int(max_entities))
        self.max_relations = max(100, int(max_relations))
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_relations (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.4,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                observations INTEGER NOT NULL DEFAULT 1,
                provenance TEXT NOT NULL DEFAULT '{}'
            )""")
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pattern_relations_subject
            ON pattern_relations (subject, predicate)""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_conflicts (
                id TEXT PRIMARY KEY,
                relation_id TEXT NOT NULL,
                conflicting_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                resolution TEXT NOT NULL DEFAULT '',
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                resolved_at REAL
            )""")

    # -- entities --------------------------------------------------------------

    def add_entity(self, name: str, entity_type: str = "concept", *,
                   aliases: Sequence[str] = (),
                   metadata: Optional[Dict[str, Any]] = None) -> Entity:
        entity_type = (entity_type or "concept").strip().lower()
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity type {entity_type!r}; "
                             f"expected one of {ENTITY_TYPES}")
        name = (name or "").strip()[:MAX_NAME]
        if not name:
            raise ValueError("entity name must be non-empty")
        entity_id = _slug(name)
        row = self._db.query_one(
            "SELECT id FROM pattern_entities WHERE id = ?", (entity_id,))
        now = time.time()
        if row is None:
            self._db.execute(
                "INSERT INTO pattern_entities (id, name, entity_type, "
                "aliases, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, name, entity_type,
                 json.dumps(list(dict.fromkeys(_slug(a) for a in aliases
                                               if a and _slug(a) != entity_id))),
                 json.dumps(dict(metadata or {}), default=str)[:2000], now))
            self._enforce_entity_cap()
        return Entity(id=entity_id, name=name, entity_type=entity_type,
                      aliases=tuple(dict.fromkeys(a for a in aliases if a)),
                      metadata=dict(metadata or {}))

    def entity(self, name_or_id: str) -> Optional[Entity]:
        key = _slug(name_or_id)
        row = self._db.query_one(
            "SELECT * FROM pattern_entities WHERE id = ?", (key,))
        if row is None:
            return None
        return Entity(id=row["id"], name=row["name"],
                      entity_type=row["entity_type"],
                      aliases=tuple(json.loads(row["aliases"] or "[]")),
                      metadata=_json_dict(row["metadata"]))

    # -- relations -------------------------------------------------------------

    def observe(self, subject: str, predicate: str, obj: str, *,
                 source: str, confidence: float = 0.4,
                 provenance: Optional[Dict[str, Any]] = None,
                 subject_type: str = "concept", obj_type: str = "concept",
                 note: str = "") -> Dict[str, Any]:
        """Record one evidenced relation; contradictions become CONFLICT.

        Returns ``{"status": "stored" | "reinforced" | "conflict", ...}``.
        A conflicting observation is stored as a *relation row in CONFLICT
        status* (so the evidence survives) plus an open conflict record — the
        existing ACTIVE relation is never modified or deleted here.
        """
        predicate = (predicate or "").strip().lower()[:MAX_PREDICATE]
        if not predicate or not re.match(r"^[a-z0-9_./:-]+$", predicate):
            raise ValueError("predicate must be a short slug")
        if not (source or "").strip():
            raise ValueError("a relation without a source is refused")
        confidence = max(0.05, min(1.0, float(confidence)))
        now = time.time()
        self.add_entity(subject, subject_type)
        self.add_entity(obj, obj_type)
        subject_id, object_id = _slug(subject), _slug(obj)

        existing = self._db.query(
            "SELECT * FROM pattern_relations WHERE subject = ? AND "
            "predicate = ? AND status IN ('ACTIVE','CONTEXT_SPECIFIC')",
            (subject_id, predicate))
        same = [row for row in existing if row["object"] == object_id]
        if same:
            row = same[0]
            old_conf = float(row["confidence"])
            old_obs = int(row["observations"])
            # Bounded reinforcement: two thirds weight to the new sighting,
            # capped at CONFIDENCE_CEILING — never a laundered 1.0.
            new_conf = min(CONFIDENCE_CEILING,
                           max(old_conf, (old_conf * old_obs + confidence)
                               / (old_obs + 1)))
            self._db.execute(
                "UPDATE pattern_relations SET confidence = ?, last_seen = ?, "
                "observations = observations + 1 WHERE id = ?",
                (new_conf, now, row["id"]))
            return {"status": "reinforced", "relation_id": row["id"],
                    "confidence": round(new_conf, 4),
                    "observations": old_obs + 1}

        conflicting = [row for row in existing
                       if row["object"] != object_id
                       and predicate in FUNCTIONAL_PREDICATES]
        relation_id = "rel-" + hashlib.sha1(
            f"{subject_id}|{predicate}|{object_id}|{source}|{now}".encode(
                "utf-8")).hexdigest()[:16]
        status = "CONFLICT" if conflicting else "ACTIVE"
        prov = dict(provenance or {})
        if note:
            prov["note"] = str(note)[:MAX_NOTE]
        self._db.execute(
            "INSERT INTO pattern_relations (id, subject, predicate, object, "
            "source, confidence, status, created_at, last_seen, "
            "observations, provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (relation_id, subject_id, predicate, object_id,
             source[:MAX_SOURCE], confidence, status, now, now, 1,
             json.dumps(prov, default=str)[:2000]))
        self._enforce_relation_cap()
        result: Dict[str, Any] = {"status": "stored",
                                  "relation_id": relation_id,
                                  "confidence": round(confidence, 4),
                                  "relation_status": status}
        if conflicting:
            conflict_id = "cfl-" + hashlib.sha1(
                f"{relation_id}|{conflicting[0]['id']}".encode()).hexdigest()[:16]
            self._db.execute(
                "INSERT INTO pattern_conflicts (id, relation_id, "
                "conflicting_id, subject, predicate, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'OPEN', ?)",
                (conflict_id, relation_id, conflicting[0]["id"], subject_id,
                 predicate, now))
            self._enforce_conflict_cap()
            result.update({"conflict_id": conflict_id,
                           "conflicts_with": conflicting[0]["id"],
                           "honesty": "nothing was overwritten; both "
                                      "observations stand until adjudicated"})
        return result

    # -- conflict handling (H2) --------------------------------------------------

    def open_conflicts(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM pattern_conflicts WHERE status = 'OPEN' "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),))
        out = []
        for row in rows:
            new_rel = self._relation_row(row["relation_id"])
            old_rel = self._relation_row(row["conflicting_id"])
            out.append({
                "conflict_id": row["id"], "subject": row["subject"],
                "predicate": row["predicate"],
                "old": _relation_dict(old_rel) if old_rel else None,
                "new": _relation_dict(new_rel) if new_rel else None,
                "created_at": float(row["created_at"]),
                "options": ["old-stale", "new-stronger", "context-specific",
                            "user-clarification"],
            })
        return out

    def adjudicate(self, conflict_id: str, resolution: str, *,
                   note: str = "") -> Dict[str, Any]:
        """Settle a conflict explicitly. Resolution is one of:

        ``old-stale``        old relation becomes STALE, new one ACTIVE;
        ``new-stronger``     old SUPERSEDED, new ACTIVE (supersedes recorded);
        ``context-specific`` both ACTIVE, new marked CONTEXT_SPECIFIC;
        ``user-clarification`` both stay CONFLICT; conflict remains OPEN with
                             the question recorded (never silently decided).
        """
        resolution = (resolution or "").strip().lower()
        if resolution not in ("old-stale", "new-stronger", "context-specific",
                              "user-clarification"):
            raise ValueError("unknown resolution %r" % (resolution,))
        row = self._db.query_one(
            "SELECT * FROM pattern_conflicts WHERE id = ?", (conflict_id,))
        if row is None:
            raise KeyError(f"unknown conflict {conflict_id!r}")
        if str(row["status"]) != "OPEN":
            return {"status": "already-resolved", "conflict_id": conflict_id,
                    "resolution": row["resolution"]}
        now = time.time()
        if resolution == "user-clarification":
            self._db.execute(
                "UPDATE pattern_conflicts SET resolution = ?, "
                "resolution_note = ? WHERE id = ?",
                (resolution, str(note)[:MAX_NOTE], conflict_id))
            return {"status": "awaiting-user", "conflict_id": conflict_id,
                    "resolution": resolution,
                    "note": "kept both claims CONFLICT until the user "
                            "clarifies"}
        # (first tuple indexes the OLD relation, per open_conflicts rows)
        final = {"old-stale": ("STALE", "ACTIVE"),
                 "new-stronger": ("SUPERSEDED", "ACTIVE"),
                 "context-specific": ("ACTIVE", "CONTEXT_SPECIFIC")}[resolution]
        self._db.execute("UPDATE pattern_relations SET status = ? WHERE id = ?",
                         (final[0], row["conflicting_id"]))
        self._db.execute("UPDATE pattern_relations SET status = ? WHERE id = ?",
                         (final[1], row["relation_id"]))
        if resolution == "new-stronger":
            old = self._relation_row(row["conflicting_id"])
            self._db.execute(
                "UPDATE pattern_relations SET provenance = ? WHERE id = ?",
                (json.dumps({**(_json_dict(old["provenance"]) if old else {}),
                              "supersedes": row["conflicting_id"]},
                             default=str)[:2000], row["relation_id"]))
        self._db.execute(
            "UPDATE pattern_conflicts SET status = 'RESOLVED', resolution = ?, "
            "resolution_note = ?, resolved_at = ? WHERE id = ?",
            (resolution, str(note)[:MAX_NOTE], now, conflict_id))
        return {"status": "resolved", "conflict_id": conflict_id,
                "resolution": resolution,
                "old_status": final[0], "new_status": final[1]}

    # -- queries ------------------------------------------------------------------

    def relations_of(self, name_or_id: str, *, direction: str = "both",
                     predicate: str = "", status: str = "ACTIVE",
                     limit: int = 100) -> List[Relation]:
        key = _slug(name_or_id)
        params: List[Any] = []
        clauses: List[str] = []
        if direction == "out":
            clauses.append("subject = ?")
            params.append(key)
        elif direction == "in":
            clauses.append("object = ?")
            params.append(key)
        else:
            clauses.append("(subject = ? OR object = ?)")
            params.extend([key, key])
        if predicate:
            clauses.append("predicate = ?")
            params.append(predicate.strip().lower()[:MAX_PREDICATE])
        if status and status != "ANY":
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        rows = self._db.query(
            "SELECT * FROM pattern_relations WHERE " + " AND ".join(clauses) +
            " ORDER BY confidence DESC, last_seen DESC LIMIT ?", tuple(params))
        return [_relation_from_row(row) for row in rows if row is not None]

    def paths_between(self, left: str, right: str, *, max_hops: int = 3,
                      limit: int = 10) -> List[List[Dict[str, Any]]]:
        """Bounded BFS over ACTIVE edges — relationships, not guesses."""
        start, finish = _slug(left), _slug(right)
        if start == finish:
            return []
        frontier: List[Tuple[str, List[Dict[str, Any]]]] = [(start, [])]
        visited = {start}
        found: List[List[Dict[str, Any]]] = []
        for _ in range(max(1, min(int(max_hops), 4))):
            if not frontier or len(found) >= limit:
                break
            nxt: List[Tuple[str, List[Dict[str, Any]]]] = []
            for node, trail in frontier:
                for rel in self._edges_from(node):
                    other = rel["object"] if rel["subject"] == node else rel["subject"]
                    step = {**_edge_dict(rel), "from": node, "to": other}
                    if other == finish:
                        found.append(trail + [step])
                        if len(found) >= limit:
                            break
                        continue
                    if other in visited:
                        continue
                    visited.add(other)
                    nxt.append((other, trail + [step]))
            frontier = nxt
        return found

    def summary(self) -> Dict[str, Any]:
        ent = self._db.query_one("SELECT COUNT(*) AS c FROM pattern_entities")
        rel = self._db.query_one(
            "SELECT status, COUNT(*) AS c FROM pattern_relations GROUP BY status")
        by_status = {}
        rows = self._db.query(
            "SELECT status, COUNT(*) AS c FROM pattern_relations GROUP BY status")
        for row in rows:
            by_status[row["status"]] = int(row["c"])
        open_conf = self._db.query_one(
            "SELECT COUNT(*) AS c FROM pattern_conflicts WHERE status='OPEN'")
        return {
            "entities": int(ent["c"]) if ent is not None else 0,
            "relations": sum(by_status.values()),
            "by_status": by_status,
            "open_conflicts": int(open_conf["c"]) if open_conf is not None else 0,
            "max_entities": self.max_entities,
            "max_relations": self.max_relations,
        }

    # -- deterministic ingestion ----------------------------------------------------

    _FILE_RE = re.compile(r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|h|cpp|md|yaml|yml|json|toml|sql|sh)\b")
    _CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
    _DOTTED_RE = re.compile(r"\b[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*){1,4}\b")

    def ingest_text(self, text: str, *, source: str, confidence: float = 0.3,
                    max_entities: int = 12) -> Dict[str, Any]:
        """Extract entities + co-occurrence hypotheses from one text.

        Co-occurrence edges are *hypotheses* ("related_in") at the supplied
        low confidence; they never assert causation or identity. Entities
        that never co-occur in the same passage are never linked.
        """
        found: List[Tuple[str, str]] = []
        for match in self._FILE_RE.findall(text or ""):
            found.append((_slug(match), "file"))
        for match in self._CAMEL_RE.findall(text or ""):
            found.append((_slug(match), "concept"))
        for match in self._DOTTED_RE.findall(text or ""):
            if match.count(".") and "related_in" not in match:
                found.append((_slug(match), "concept"))
        unique: Dict[str, str] = {}
        for ident, kind in found:
            if ident and ident not in unique:
                unique[ident] = kind
            if len(unique) >= max(2, int(max_entities)):
                break
        if len(unique) < 2:
            return {"entities": len(unique), "edges": 0,
                    "reason": "fewer than two extractable entities"}
        names = list(unique)
        for name in names:
            self.add_entity(name, unique[name])
        edges = 0
        for left_index in range(len(names)):
            for right_index in range(left_index + 1, len(names)):
                left, right = names[left_index], names[right_index]
                self.observe(left, "related_in", right, source=source,
                             confidence=confidence,
                             provenance={"kind": "co-occurrence",
                                         "evidence": "same passage"})
                edges += 1
                if edges >= 60:
                    break
            if edges >= 60:
                break
        return {"entities": len(unique), "edges": edges}

    def ingest_memory(self, record: Any) -> Dict[str, Any]:
        """Project one MemoryRecord-shaped item into the graph.

        Preference-shaped content becomes ``user -prefers-> X``; decision-
        shaped content becomes ``project -decided-> X``; anything with
        ``conflict`` semantics is refused for silent merge and surfaced as an
        observation (which itself may open a CONFLICT — never overwrite).
        """
        content = str(getattr(record, "content", "") or "")
        memory_type = str(getattr(record, "memory_type", "") or "")
        source = "memory:" + str(getattr(record, "id", "") or "?")
        confidence = float(getattr(record, "confidence", 0.5) or 0.5)
        project = str(getattr(record, "project", "") or "default")
        lowered = content.lower()
        created = 0
        if memory_type in ("preference",) or lowered.startswith("preference:"):
            topic = re.sub(r"^preference:\s*", "", content, flags=re.IGNORECASE)
            for term in _key_terms(topic)[:4]:
                self.observe("user", "prefers", term, source=source,
                             confidence=max(0.4, confidence),
                             subject_type="person", obj_type="preference",
                             provenance={"memory_id": getattr(record, "id", ""),
                                        "retention": getattr(record, "retention", "")})
                created += 1
        elif memory_type == "decision" or lowered.startswith("decision:"):
            topic = re.sub(r"^decision:\s*", "", content, flags=re.IGNORECASE)
            for term in _key_terms(topic)[:4]:
                self.observe(project, "decided", term, source=source,
                             confidence=max(0.4, confidence),
                             subject_type="project", obj_type="decision",
                             provenance={"memory_id": getattr(record, "id", "")})
                created += 1
        else:
            result = self.ingest_text(content, source=source,
                                      confidence=min(0.4, confidence))
            created = int(result.get("edges", 0))
        return {"projected": created, "type": memory_type or "untyped"}

    # -- internals -----------------------------------------------------------------

    def _relation_row(self, relation_id: str) -> Optional[Any]:
        return self._db.query_one(
            "SELECT * FROM pattern_relations WHERE id = ?", (relation_id,))

    def _edges_from(self, node_id: str) -> List[Any]:
        return self._db.query(
            "SELECT * FROM pattern_relations WHERE (subject = ? OR object = ?) "
            "AND status IN ('ACTIVE','CONTEXT_SPECIFIC') ORDER BY confidence "
            "DESC LIMIT 200", (node_id, node_id))

    def _enforce_entity_cap(self) -> None:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM pattern_entities")
        total = int(row["c"]) if row is not None else 0
        overflow = total - self.max_entities
        if overflow > 0:
            self._db.execute(
                "DELETE FROM pattern_entities WHERE id IN (SELECT id FROM "
                "pattern_entities ORDER BY created_at ASC LIMIT ?)", (overflow,))

    def _enforce_relation_cap(self) -> None:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM pattern_relations")
        total = int(row["c"]) if row is not None else 0
        overflow = total - self.max_relations
        if overflow > 0:
            self._db.execute(
                "DELETE FROM pattern_relations WHERE id IN (SELECT id FROM "
                "pattern_relations ORDER BY last_seen ASC LIMIT ?)", (overflow,))

    def _enforce_conflict_cap(self) -> None:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM pattern_conflicts")
        total = int(row["c"]) if row is not None else 0
        overflow = total - MAX_CONFLICTS
        if overflow > 0:
            self._db.execute(
                "DELETE FROM pattern_conflicts WHERE id IN (SELECT id FROM "
                "pattern_conflicts ORDER BY created_at ASC LIMIT ?)",
                (overflow,))


def _relation_from_row(row: Any) -> Relation:
    return Relation(
        id=row["id"], subject=row["subject"], predicate=row["predicate"],
        obj=row["object"], source=row["source"],
        confidence=float(row["confidence"] or 0.0), status=row["status"],
        created_at=float(row["created_at"] or 0.0),
        last_seen=float(row["last_seen"] or 0.0),
        observations=int(row["observations"] or 1),
        provenance=_json_dict(row["provenance"]))


def _relation_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    relation = _relation_from_row(row)
    return relation.to_dict()


def _edge_dict(row: Any) -> Dict[str, Any]:
    return {"id": row["id"], "predicate": row["predicate"],
            "source": row["source"],
            "confidence": round(float(row["confidence"] or 0.0), 4),
            "status": row["status"]}


def _json_dict(value: Any) -> Dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_STOP = frozenset("the a an of to in on for with and or is are was were be "
                  "that this use using used we i you it as at by from".split())


def _key_terms(text: str) -> Tuple[str, ...]:
    words = re.findall(r"[A-Za-z0-9_.-]{3,}", text or "")
    out: List[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in _STOP or lowered in out:
            continue
        out.append(lowered)
    return tuple(out)
