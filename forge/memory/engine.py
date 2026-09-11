"""SQLite-backed long-term memory engine for Forge.

This is the production store behind every memory layer (session, task,
project, failure, decision, agent, model-performance). It is built on the
same shared SQLite connection the rest of the control plane uses
(:class:`forge.control.db.Database`), so memory survives restarts and is
visible to the planner, context engine, debugger, reviewer, model router,
desktop viewer, and CLI alike.

Guarantees, in order of importance:

* **No secrets.** Content is scanned and redacted *before* it is written;
  a span that is nothing but a secret is refused outright.
* **Project isolation.** Every read/write is scoped to a project; one
  project can never observe another's memory.
* **No duplicates.** Exact and near-duplicate items are merged into the
  original (with a recorded provenance event) instead of stored again.
* **No flooding.** Per-project/per-type caps evict the least important,
  oldest items; content below a signal floor is rejected.
* **Provenance.** Every lifecycle action (create/correct/supersede/merge/
  summarize/delete/purge/expire) is appended to an append-only audit table.
* **Retention.** Each item carries an expiry policy; expired items stop
  being returned and can be reclaimed deterministically.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.memory.redaction import scan_secrets
from forge.memory.relevance import (
    RelevanceRanker,
    content_tokens,
    fingerprint,
    jaccard,
    normalize,
    signal_ratio,
)
from forge.memory.summarizer import summarize_records
from forge.memory.types import (
    ACTION_CORRECTED,
    ACTION_CREATED,
    ACTION_DELETED,
    ACTION_EXPIRED,
    ACTION_MERGED,
    ACTION_PURGED,
    ACTION_REJECTED,
    ACTION_SUMMARIZED,
    ACTION_SUPERSEDED,
    ACTIVE,
    DEFAULT_RETENTION_BY_TYPE,
    DEFAULT_TTL_SECONDS,
    DELETED,
    EXPIRED,
    SUPERSEDED,
    MemoryRecord,
    MemoryType,
    RememberResult,
    Retention,
    SearchResult,
)

DEFAULT_PROJECT = "default"
MAX_CONTENT_CHARS = 100_000
MIN_CONTENT_TOKENS = 3
MAX_STOPWORD_RATIO = 0.9
DUPLICATE_JACCARD_THRESHOLD = 0.9
DUPLICATE_SCAN_LIMIT = 200
MAX_ENTRIES_PER_PROJECT = 10_000
MAX_ENTRIES_PER_TYPE = 5_000
MAX_PROVENANCE_DETAIL_CHARS = 400


class MemoryConfig:
    """Bounds for flood prevention, dedupe, and retention enforcement."""

    def __init__(
        self,
        *,
        max_content_chars: int = MAX_CONTENT_CHARS,
        min_content_tokens: int = MIN_CONTENT_TOKENS,
        max_stopword_ratio: float = MAX_STOPWORD_RATIO,
        duplicate_jaccard_threshold: float = DUPLICATE_JACCARD_THRESHOLD,
        duplicate_scan_limit: int = DUPLICATE_SCAN_LIMIT,
        max_entries_per_project: int = MAX_ENTRIES_PER_PROJECT,
        max_entries_per_type: int = MAX_ENTRIES_PER_TYPE,
    ) -> None:
        self.max_content_chars = max_content_chars
        self.min_content_tokens = min_content_tokens
        self.max_stopword_ratio = max_stopword_ratio
        self.duplicate_jaccard_threshold = duplicate_jaccard_threshold
        self.duplicate_scan_limit = duplicate_scan_limit
        self.max_entries_per_project = max_entries_per_project
        self.max_entries_per_type = max_entries_per_type


def _now() -> float:
    return time.time()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    return max(low, min(high, number))


def _ttl_seconds(retention: Retention,
                 ttl_seconds: Optional[float]) -> Optional[float]:
    if ttl_seconds is not None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        return float(ttl_seconds)
    return DEFAULT_TTL_SECONDS.get(retention.value, None)


class MemoryNotFoundError(KeyError):
    """Raised when a memory id is unknown or invisible to the caller."""


class LongTermMemory:
    """Durable, typed, project-scoped long-term memory over SQLite."""

    def __init__(self, db: Any = None, *, project: Optional[str] = None,
                 config: Optional[MemoryConfig] = None) -> None:
        if db is None:
            raise ValueError("LongTermMemory requires a Database or a path")
        if isinstance(db, (str, Path)):
            from forge.control.db import Database
            db = Database(db)
        self._db = db
        self.project = project or DEFAULT_PROJECT
        self.config = config or MemoryConfig()
        self._create_schema()

    # -- schema ---------------------------------------------------------

    def _create_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS long_term_memory (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                project TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                via TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                retention TEXT NOT NULL,
                expires_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                version INTEGER NOT NULL DEFAULT 1,
                supersedes TEXT NOT NULL DEFAULT '',
                redaction_count INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_project_type "
            "ON long_term_memory(project, memory_type, created_at)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_status "
            "ON long_term_memory(status)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_fingerprint "
            "ON long_term_memory(fingerprint)")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS long_term_memory_provenance (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_provenance "
            "ON long_term_memory_provenance(memory_id, created_at)")

    # -- helpers ---------------------------------------------------------

    def _effective_project(self, project: Optional[str]) -> str:
        return project or self.project or DEFAULT_PROJECT

    @staticmethod
    def _from_row(row: Any) -> MemoryRecord:
        metadata: Dict[str, Any] = {}
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return MemoryRecord(
            id=row["id"],
            memory_type=row["memory_type"],
            project=row["project"],
            content=row["content"],
            summary=row["summary"] or "",
            source=row["source"] or "",
            via=row["via"] or "",
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            retention=row["retention"],
            expires_at=float(row["expires_at"])
            if row["expires_at"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_accessed_at=float(row["last_accessed_at"]),
            access_count=int(row["access_count"]),
            fingerprint=row["fingerprint"],
            status=row["status"],
            version=int(row["version"]),
            supersedes=row["supersedes"] or "",
            metadata=metadata,
            redaction_count=int(row["redaction_count"]),
        )

    _ROW_COLUMNS = (
        "id, memory_type, project, content, summary, source, via, "
        "confidence, importance, retention, expires_at, created_at, "
        "updated_at, last_accessed_at, access_count, fingerprint, "
        "status, version, supersedes, redaction_count, metadata"
    )

    def _record_provenance(self, memory_id: str, action: str, *,
                           actor: str = "", detail: str = "") -> None:
        safe_detail = (detail or "")[:MAX_PROVENANCE_DETAIL_CHARS]
        self._db.execute(
            "INSERT INTO long_term_memory_provenance "
            "(memory_id, action, actor, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (memory_id, action, (actor or "")[:128], safe_detail, _now()))

    # -- ingestion -------------------------------------------------------

    def remember(self, memory_type: Any, content: str, *,
                 project: Optional[str] = None, source: str = "",
                 via: str = "", confidence: float = 0.5,
                 importance: float = 0.5, retention: Any = None,
                 summary: str = "", metadata: Optional[Dict[str, Any]] = None,
                 ttl_seconds: Optional[float] = None) -> RememberResult:
        """Store one memory item with all guards applied.

        Returns a :class:`RememberResult` describing whether the item was
        stored, merged into a duplicate, or rejected by the flood guard.
        """
        memory_type = MemoryType.parse(memory_type)
        retention = Retention.parse(retention if retention is not None
                                    else DEFAULT_RETENTION_BY_TYPE.get(
                                        memory_type.value, Retention.PROJECT))
        project = self._effective_project(project)

        if not isinstance(content, str) or not content.strip():
            return RememberResult(
                status="rejected", reason="content must be a non-empty string")
        if len(content) > self.config.max_content_chars:
            return RememberResult(
                status="rejected",
                reason=f"content exceeds the {self.config.max_content_chars}"
                       f"-char bound")

        # 1. Secrets: redact spans in place; refuse pure-secret content.
        scan = scan_secrets(content)
        if scan.is_entirely_secret:
            return RememberResult(
                status="rejected",
                reason="content is a secret and will never be stored")
        redacted = scan.redacted_text

        # 2. Flood guard: refuse content below the signal floor (too few
        # meaningful tokens, or dominated by stop words).
        signal = signal_ratio(redacted)
        tokens = content_tokens(redacted)
        if len(tokens) < self.config.min_content_tokens:
            return RememberResult(
                status="rejected",
                reason=f"content carries fewer than "
                       f"{self.config.min_content_tokens} meaningful tokens")
        if signal <= (1.0 - self.config.max_stopword_ratio):
            return RememberResult(
                status="rejected",
                reason="content is stop-word noise (low information signal)")

        # 3. Dedupe: exact fingerprint first, then bounded near-duplicate scan.
        fp = fingerprint(redacted)
        existing = self._db.query_one(
            "SELECT id, project, memory_type FROM long_term_memory "
            "WHERE fingerprint = ? AND project = ? AND status = ? "
            "LIMIT 1", (fp, project, ACTIVE))
        if existing is not None:
            self._record_provenance(
                existing["id"], ACTION_MERGED, actor=source or via,
                detail=f"duplicate observed (fingerprint)")
            return RememberResult(
                status="duplicate", duplicate_of=existing["id"],
                reason="exact duplicate of an existing item")

        near = self._find_near_duplicate(project, memory_type.value, redacted)
        if near is not None:
            self._record_provenance(
                near["id"], ACTION_MERGED, actor=source or via,
                detail="near-duplicate observed")
            return RememberResult(
                status="duplicate", duplicate_of=near["id"],
                reason="near-duplicate of an existing item")

        # 4. Store the item.
        now = _now()
        ttl = _ttl_seconds(retention, ttl_seconds)
        expires_at = now + ttl if ttl is not None else None
        record_id = uuid.uuid4().hex
        metadata_json = json.dumps(dict(metadata or {}), default=str)
        summary = (summary or "")[:self.config.max_content_chars]
        self._db.execute(
            "INSERT INTO long_term_memory (id, memory_type, project, content, "
            "summary, source, via, confidence, importance, retention, "
            "expires_at, created_at, updated_at, last_accessed_at, "
            "access_count, fingerprint, status, version, supersedes, "
            "redaction_count, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (record_id, memory_type.value, project, redacted, summary,
             (source or "")[:128], (via or "")[:128],
             _clamp(confidence), _clamp(importance), retention.value,
             expires_at, now, now, now, 0, fp, ACTIVE, 1, "",
             scan.redaction_count, metadata_json))
        self._record_provenance(
            record_id, ACTION_CREATED, actor=source or via,
            detail=f"type={memory_type.value} retention={retention.value} "
                   f"redactions={scan.redaction_count}")
        record = self._load(record_id)
        self._enforce_caps(project)
        return RememberResult(status="stored", record=record)

    def _find_near_duplicate(self, project: str, memory_type: str,
                             content: str) -> Optional[Any]:
        """Bounded scan for near-duplicates among recent active items."""
        tokens = set(content_tokens(content))
        if not tokens:
            return None
        rows = self._db.query(
            "SELECT id, content FROM long_term_memory WHERE project = ? "
            "AND memory_type = ? AND status = ? ORDER BY created_at DESC "
            "LIMIT ?", (project, memory_type, ACTIVE,
                        self.config.duplicate_scan_limit))
        for row in rows:
            if jaccard(tokens, content_tokens(row["content"])) \
                    >= self.config.duplicate_jaccard_threshold:
                return row
        return None

    def _enforce_caps(self, project: str) -> None:
        """Evict least-important/oldest active items beyond the hard caps."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM long_term_memory "
            "WHERE project = ? AND status = ?", (project, ACTIVE))
        if row is not None and int(row["n"]) > self.config.max_entries_per_project:
            self._evict(project, None,
                        int(row["n"]) - self.config.max_entries_per_project)
        for memory_type in MemoryType.values():
            typed = self._db.query_one(
                "SELECT COUNT(*) AS n FROM long_term_memory "
                "WHERE project = ? AND memory_type = ? AND status = ?",
                (project, memory_type, ACTIVE))
            if typed is not None and int(typed["n"]) > self.config.max_entries_per_type:
                self._evict(project, memory_type,
                            int(typed["n"]) - self.config.max_entries_per_type)

    def _evict(self, project: str, memory_type: Optional[str],
               count: int) -> None:
        """Remove ``count`` active items, least important/oldest first."""
        params: List[Any] = [project, ACTIVE]
        clause = "project = ? AND status = ?"
        if memory_type is not None:
            clause += " AND memory_type = ?"
            params.append(memory_type)
        params.append(max(0, count))
        victims = self._db.query(
            f"SELECT id FROM long_term_memory WHERE {clause} "
            "ORDER BY importance ASC, created_at ASC LIMIT ?", tuple(params))
        for victim in victims:
            self._mark_status(victim["id"], EXPIRED, ACTION_EXPIRED,
                              detail="evicted by memory cap")
            self._db.execute(
                "DELETE FROM long_term_memory WHERE id = ?", (victim["id"],))

    # -- retrieval -------------------------------------------------------

    def _load(self, memory_id: str) -> Optional[MemoryRecord]:
        row = self._db.query_one(
            f"SELECT {self._ROW_COLUMNS} FROM long_term_memory "
            "WHERE id = ?", (memory_id,))
        return self._from_row(row) if row is not None else None

    def get(self, memory_id: str, *, project: Optional[str] = None,
            include_inactive: bool = False) -> Optional[MemoryRecord]:
        """Fetch one item, bumping access metadata (project-scoped)."""
        project = self._effective_project(project)
        record = self._load(memory_id)
        if record is None or record.project != project:
            return None
        if record.status == EXPIRED and not include_inactive:
            return None
        if record.status == DELETED and not include_inactive:
            return None
        if record.expires_at is not None and record.expires_at <= _now() \
                and record.status == ACTIVE:
            self._mark_status(record.id, EXPIRED, ACTION_EXPIRED,
                              detail="expiry reached")
            if not include_inactive:
                return None
        self._db.execute(
            "UPDATE long_term_memory SET access_count = access_count + 1, "
            "last_accessed_at = ? WHERE id = ?", (_now(), memory_id))
        return self._load(memory_id)

    def list(self, *, project: Optional[str] = None,
             memory_type: Any = None, limit: int = 100,
             include_inactive: bool = False) -> List[MemoryRecord]:
        """Newest-first listing, project-scoped, expired items filtered."""
        project = self._effective_project(project)
        self.enforce_retention(project=project)
        conditions = ["project = ?"]
        params: List[Any] = [project]
        if memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(MemoryType.parse(memory_type).value)
        if not include_inactive:
            conditions.append("status = ?")
            params.append(ACTIVE)
        params.append(max(1, min(int(limit), 1000)))
        rows = self._db.query(
            f"SELECT {self._ROW_COLUMNS} FROM long_term_memory WHERE "
            f"{' AND '.join(conditions)} ORDER BY created_at DESC, id DESC "
            "LIMIT ?", tuple(params))
        return [self._from_row(row) for row in rows]

    def search(self, query: str, *, project: Optional[str] = None,
               memory_type: Any = None, k: int = 10,
               min_score: float = 0.0) -> List[SearchResult]:
        """Relevance-ranked retrieval over project-scoped, active items."""
        project = self._effective_project(project)
        self.enforce_retention(project=project)
        candidates = self.list(project=project, memory_type=memory_type,
                               limit=1000, include_inactive=False)
        if not query or not query.strip():
            return []
        ranker = RelevanceRanker(candidates)
        return ranker.rank(query, k=max(1, min(int(k), 100)),
                           min_score=min_score)

    def recall(self, query: str, *, project: Optional[str] = None,
               memory_type: Any = None, k: int = 5) -> List[SearchResult]:
        """Convenience alias for context/planner integrations."""
        return self.search(query, project=project, memory_type=memory_type,
                           k=k)

    # -- correction & deletion -------------------------------------------

    def correct(self, memory_id: str, new_content: str, *,
                source: str = "", reason: str = "",
                confidence: Optional[float] = None,
                importance: Optional[float] = None,
                project: Optional[str] = None) -> MemoryRecord:
        """Correct an existing item by superseding it with a new version.

        The original is kept (status ``superseded``) so the audit trail is
        never destroyed; the new version links back through ``supersedes``.
        """
        project = self._effective_project(project)
        original = self._load(memory_id)
        if original is None or original.project != project:
            raise MemoryNotFoundError(
                f"unknown memory id in project {project!r}: {memory_id!r}")
        if original.status == DELETED:
            raise MemoryNotFoundError(
                f"memory item is deleted: {memory_id!r}")

        if not isinstance(new_content, str) or not new_content.strip():
            raise ValueError("corrected content must be a non-empty string")
        scan = scan_secrets(new_content)
        if scan.is_entirely_secret:
            raise ValueError("corrected content is a secret and will not be "
                             "stored")
        redacted = scan.redacted_text
        if len(redacted) > self.config.max_content_chars:
            raise ValueError("corrected content exceeds the size bound")

        now = _now()
        new_id = uuid.uuid4().hex
        new_version = original.version + 1
        new_confidence = _clamp(confidence if confidence is not None
                                else original.confidence)
        new_importance = _clamp(importance if importance is not None
                                else original.importance)
        self._db.execute(
            "INSERT INTO long_term_memory (id, memory_type, project, content, "
            "summary, source, via, confidence, importance, retention, "
            "expires_at, created_at, updated_at, last_accessed_at, "
            "access_count, fingerprint, status, version, supersedes, "
            "redaction_count, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (new_id, original.memory_type, project, redacted,
             original.summary, (source or original.source)[:128],
             original.via, new_confidence, new_importance,
             original.retention, original.expires_at, now, now, now, 0,
             fingerprint(redacted), ACTIVE, new_version, original.id,
             scan.redaction_count, json.dumps(
                 {"corrected_from": original.id}, default=str)))
        self._db.execute(
            "UPDATE long_term_memory SET status = ?, updated_at = ? "
            "WHERE id = ?", (SUPERSEDED, now, original.id))
        self._record_provenance(
            new_id, ACTION_CORRECTED, actor=source or "memory-corrector",
            detail=(reason or "corrected")[:MAX_PROVENANCE_DETAIL_CHARS])
        self._record_provenance(
            original.id, ACTION_SUPERSEDED, actor=source or "memory-corrector",
            detail=f"superseded by {new_id}")
        return self._load(new_id)

    def delete(self, memory_id: str, *, source: str = "",
               reason: str = "", project: Optional[str] = None) -> bool:
        """Soft-delete an item (auditable; content is no longer returned)."""
        project = self._effective_project(project)
        record = self._load(memory_id)
        if record is None or record.project != project:
            return False
        if record.status == DELETED:
            return False
        self._db.execute(
            "UPDATE long_term_memory SET status = ?, updated_at = ? "
            "WHERE id = ?", (DELETED, _now(), memory_id))
        self._record_provenance(
            memory_id, ACTION_DELETED, actor=source or "memory-operator",
            detail=(reason or "deleted")[:MAX_PROVENANCE_DETAIL_CHARS])
        return True

    def purge(self, memory_id: str, *, source: str = "",
              reason: str = "", project: Optional[str] = None) -> bool:
        """Hard-delete an item; its provenance trail is preserved."""
        project = self._effective_project(project)
        record = self._load(memory_id)
        if record is None or record.project != project:
            return False
        self._record_provenance(
            memory_id, ACTION_PURGED, actor=source or "memory-operator",
            detail=(reason or "purged")[:MAX_PROVENANCE_DETAIL_CHARS])
        self._db.execute(
            "DELETE FROM long_term_memory WHERE id = ?", (memory_id,))
        return True

    def _mark_status(self, memory_id: str, status: str, action: str, *,
                     detail: str = "") -> None:
        self._db.execute(
            "UPDATE long_term_memory SET status = ?, updated_at = ? "
            "WHERE id = ?", (status, _now(), memory_id))
        self._record_provenance(memory_id, action, detail=detail)

    # -- summarization ---------------------------------------------------

    def summarize(self, *, project: Optional[str] = None,
                  memory_type: Any = None, max_sentences: int = 3,
                  since: Optional[float] = None, limit: int = 50,
                  source: str = "memory-summarizer") -> Optional[MemoryRecord]:
        """Compress recent items into one durable, extractive summary item."""
        project = self._effective_project(project)
        self.enforce_retention(project=project)
        conditions = ["project = ?", "status = ?"]
        params: List[Any] = [project, ACTIVE]
        if memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(MemoryType.parse(memory_type).value)
        if since is not None:
            conditions.append("created_at >= ?")
            params.append(float(since))
        params.append(max(1, min(int(limit), 200)))
        rows = self._db.query(
            f"SELECT {self._ROW_COLUMNS} FROM long_term_memory WHERE "
            f"{' AND '.join(conditions)} ORDER BY created_at DESC LIMIT ?",
            tuple(params))
        records = [self._from_row(row) for row in rows]
        if len(records) < 2:
            return None
        summary_text = summarize_records(records,
                                         max_sentences=max_sentences)
        if not summary_text:
            return None
        result = self.remember(
            (MemoryType.parse(memory_type).value
             if memory_type is not None else MemoryType.PROJECT.value),
            summary_text,
            project=project,
            source=source,
            via="summarization",
            confidence=sum(r.confidence for r in records) / len(records),
            importance=sum(r.importance for r in records) / len(records),
            retention=Retention.PROJECT,
            metadata={"summarized_ids": [r.id for r in records],
                      "summarized_count": len(records)},
        )
        if result.stored and result.record is not None:
            self._record_provenance(
                result.record.id, ACTION_SUMMARIZED, actor=source,
                detail=f"summarized {len(records)} items")
        return result.record

    # -- retention -------------------------------------------------------

    def enforce_retention(self, *, project: Optional[str] = None,
                          now: Optional[float] = None) -> int:
        """Expire items past their retention policy; returns count expired."""
        project = self._effective_project(project)
        moment = _now() if now is None else now
        rows = self._db.query(
            "SELECT id FROM long_term_memory WHERE project = ? "
            "AND status = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (project, ACTIVE, moment))
        for row in rows:
            self._mark_status(row["id"], EXPIRED, ACTION_EXPIRED,
                              detail="retention policy reached")
        return len(rows)

    def purge_expired(self, *, project: Optional[str] = None) -> int:
        """Hard-delete expired items (their provenance rows remain)."""
        project = self._effective_project(project)
        rows = self._db.query(
            "SELECT id FROM long_term_memory WHERE project = ? "
            "AND status = ?", (project, EXPIRED))
        for row in rows:
            self._record_provenance(row["id"], ACTION_PURGED,
                                    actor="retention",
                                    detail="expired item reclaimed")
            self._db.execute(
                "DELETE FROM long_term_memory WHERE id = ?", (row["id"],))
        return len(rows)

    # -- provenance & stats ----------------------------------------------

    def provenance(self, memory_id: str) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT memory_id, action, actor, detail, created_at "
            "FROM long_term_memory_provenance WHERE memory_id = ? "
            "ORDER BY seq ASC", (memory_id,))
        return [dict(row) for row in rows]

    def stats(self, *, project: Optional[str] = None) -> Dict[str, Any]:
        project = self._effective_project(project)
        by_type: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        for row in self._db.query(
                "SELECT memory_type, status, COUNT(*) AS n "
                "FROM long_term_memory WHERE project = ? GROUP BY "
                "memory_type, status", (project,)):
            by_type[row["memory_type"]] = \
                by_type.get(row["memory_type"], 0) + int(row["n"])
            by_status[row["status"]] = \
                by_status.get(row["status"], 0) + int(row["n"])
        total = sum(by_status.values())
        redactions = self._db.query_one(
            "SELECT COALESCE(SUM(redaction_count), 0) AS n "
            "FROM long_term_memory WHERE project = ?", (project,))
        provenance = self._db.query_one(
            "SELECT COUNT(*) AS n FROM long_term_memory_provenance")
        return {
            "project": project,
            "total": total,
            "active": by_status.get(ACTIVE, 0),
            "deleted": by_status.get(DELETED, 0),
            "expired": by_status.get(EXPIRED, 0),
            "superseded": by_status.get(SUPERSEDED, 0),
            "by_type": by_type,
            "by_status": by_status,
            "redactions": int(redactions["n"]) if redactions else 0,
            "provenance_events": int(provenance["n"]) if provenance else 0,
        }
