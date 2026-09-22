"""Prompt versioning ledger (A84 Stage D4).

Tracks, per execution: original prompt (bounded), enhanced prompt, the
model-targeted prompt, the execution result and quality metrics. The ledger
is deliberately *storage-honest*:

* it stores what was shown to the model, never hidden system instructions;
* it never stores reasoning traces or chain-of-thought content;
* it redacts secret-shaped spans through the existing memory redaction;
* it is bounded (FIFO) so it cannot grow without limit;
* rows are keyed by trace/task/session ids so outcomes can be joined with
  runs, learning ledgers and the audit log.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from forge.memory.redaction import scan_secrets

__all__ = ["PromptLedger", "PromptVersion"]

MAX_LEDGER_ROWS = 500
MAX_TEXT = 6000
MAX_META = 1500


@dataclass(frozen=True)
class PromptVersion:
    """One recorded enhancement/execution chain."""

    version_id: str
    trace_id: str
    session_id: str
    task_id: str
    model: str
    original_hash: str
    original_preview: str
    enhanced_preview: str
    targeted_preview: str
    quality_score: float
    verdict: str
    intent_preserved: bool
    outcome: str
    result_preview: str
    latency_ms: float
    redactions: int
    created_at: float
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "model": self.model,
            "original_hash": self.original_hash,
            "original_preview": self.original_preview,
            "enhanced_preview": self.enhanced_preview,
            "targeted_preview": self.targeted_preview,
            "quality": {"score": self.quality_score, "verdict": self.verdict,
                        "intent_preserved": self.intent_preserved},
            "outcome": self.outcome,
            "result_preview": self.result_preview,
            "latency_ms": self.latency_ms,
            "redactions": self.redactions,
            "created_at": self.created_at,
            "metadata": dict(self.metadata or {}),
        }


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _bounded_redacted(text: str) -> tuple:
    scan = scan_secrets(text or "")
    return scan.redacted_text[:MAX_TEXT], scan.redaction_count


class PromptLedger:
    """Bounded SQLite ledger of prompt chains (same Database as the plane)."""

    def __init__(self, db: Any, *, max_rows: int = MAX_LEDGER_ROWS) -> None:
        self._db = db
        self.max_rows = max(10, int(max_rows))
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                version_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                original_hash TEXT NOT NULL DEFAULT '',
                original_preview TEXT NOT NULL DEFAULT '',
                enhanced_preview TEXT NOT NULL DEFAULT '',
                targeted_preview TEXT NOT NULL DEFAULT '',
                quality_score REAL NOT NULL DEFAULT 0.0,
                verdict TEXT NOT NULL DEFAULT '',
                intent_preserved INTEGER NOT NULL DEFAULT 1,
                outcome TEXT NOT NULL DEFAULT '',
                result_preview TEXT NOT NULL DEFAULT '',
                latency_ms REAL NOT NULL DEFAULT 0.0,
                redactions INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_prompt_versions_trace "
                   "ON prompt_versions (trace_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_prompt_versions_session "
                   "ON prompt_versions (session_id, created_at)")

    # -- write -------------------------------------------------------------

    def record(self, *, enhanced: Any = None, targeted: Any = None,
               trace_id: str = "", session_id: str = "", task_id: str = "",
               model: str = "", quality: Any = None, outcome: str = "",
               result: str = "", latency_ms: float = 0.0,
               metadata: Optional[Dict[str, Any]] = None) -> PromptVersion:
        original = str(getattr(enhanced, "original", "") or "")
        enhanced_text = str(getattr(enhanced, "enhanced", "") or "")
        targeted_text = str(getattr(targeted, "prompt", "") or "")
        original_view, red_a = _bounded_redacted(original)
        enhanced_view, red_b = _bounded_redacted(enhanced_text)
        targeted_view, red_c = _bounded_redacted(targeted_text)
        result_view, red_d = _bounded_redacted(result)
        version_id = "%d-%s" % (int(time.time() * 1000),
                                _hash(original + enhanced_text + model)[:8])
        meta_text = json.dumps(dict(metadata or {}), default=str)[:MAX_META]
        self._db.execute(
            "INSERT INTO prompt_versions (version_id, trace_id, "
            "session_id, task_id, model, original_hash, original_preview, "
            "enhanced_preview, targeted_preview, quality_score, verdict, "
            "intent_preserved, outcome, result_preview, latency_ms, "
            "redactions, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, trace_id[:64], session_id[:64], task_id[:64],
             model[:120], _hash(original), original_view, enhanced_view,
             targeted_view,
             float(getattr(quality, "score", 0.0) or 0.0),
             str(getattr(quality, "verdict", "") or ""),
             1 if bool(getattr(enhanced, "intent_preserved", True)) else 0,
             outcome[:48], result_view[:1200], float(latency_ms or 0.0),
             red_a + red_b + red_c + red_d, time.time(), meta_text))
        self._prune()
        return self.get(version_id)  # type: ignore[return-value]

    def record_result(self, version_id: str, *, outcome: str,
                      result: str = "", latency_ms: float = 0.0) -> bool:
        """Attach an execution outcome to a previously recorded chain."""
        view, _ = _bounded_redacted(result)
        cursor = self._db.execute(
            "UPDATE prompt_versions SET outcome = ?, result_preview = ?, "
            "latency_ms = ? WHERE version_id = ?",
            (outcome[:48], view[:1200], float(latency_ms or 0.0), version_id))
        return cursor.rowcount > 0

    # -- read --------------------------------------------------------------

    def get(self, version_id: str) -> Optional[PromptVersion]:
        row = self._db.query_one(
            "SELECT * FROM prompt_versions WHERE version_id = ?",
            (version_id,))
        return self._from_row(row) if row is not None else None

    def for_session(self, session_id: str, *, limit: int = 25) -> List[PromptVersion]:
        rows = self._db.query(
            "SELECT * FROM prompt_versions WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (session_id[:64],
                                                 max(1, min(int(limit), 200))))
        return [self._from_row(row) for row in rows]

    def for_trace(self, trace_id: str) -> List[PromptVersion]:
        rows = self._db.query(
            "SELECT * FROM prompt_versions WHERE trace_id = ? "
            "ORDER BY created_at ASC", (trace_id[:64],))
        return [self._from_row(row) for row in rows]

    def recent(self, *, limit: int = 25) -> List[PromptVersion]:
        rows = self._db.query(
            "SELECT * FROM prompt_versions ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),))
        return [self._from_row(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total, AVG(quality_score) AS avg_score, "
            "SUM(CASE WHEN intent_preserved = 0 THEN 1 ELSE 0 END) "
            "AS lost_intent FROM prompt_versions")
        total = int(row["total"] or 0) if row is not None else 0
        return {
            "rows": total,
            "max_rows": self.max_rows,
            "avg_quality": round(float(row["avg_score"] or 0.0), 4)
            if row is not None and row["avg_score"] is not None else None,
            "intent_preservation_failures": int(
                row["lost_intent"] or 0) if row is not None else 0,
            "never_stores": ["system instructions", "reasoning traces",
                             "credentials"],
        }

    # -- internals -----------------------------------------------------------

    def _from_row(self, row: Any) -> PromptVersion:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return PromptVersion(
            version_id=row["version_id"], trace_id=row["trace_id"],
            session_id=row["session_id"], task_id=row["task_id"],
            model=row["model"], original_hash=row["original_hash"],
            original_preview=row["original_preview"],
            enhanced_preview=row["enhanced_preview"],
            targeted_preview=row["targeted_preview"],
            quality_score=float(row["quality_score"] or 0.0),
            verdict=row["verdict"],
            intent_preserved=bool(row["intent_preserved"]),
            outcome=row["outcome"], result_preview=row["result_preview"],
            latency_ms=float(row["latency_ms"] or 0.0),
            redactions=int(row["redactions"] or 0),
            created_at=float(row["created_at"] or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {})

    def _prune(self) -> None:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total FROM prompt_versions")
        total = int(row["total"]) if row is not None else 0
        overflow = total - self.max_rows
        if overflow > 0:
            self._db.execute(
                "DELETE FROM prompt_versions WHERE version_id IN ("
                "SELECT version_id FROM prompt_versions "
                "ORDER BY created_at ASC LIMIT ?)", (overflow,))
