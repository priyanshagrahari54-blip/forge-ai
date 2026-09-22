"""Operational learning (A84 Stage I1).

Records *what actually happened* across the platform — tool invocations,
model-route outcomes, provider latency, task success/failure, error
patterns — into one bounded, queryable ledger so planners and routers can
consult evidence instead of folklore.

Learning here is statistics, not autonomy: consumers (routing priors,
planner hints, improvement proposals) read aggregates. The ledger itself
changes nothing, and no consumer may use it to bypass a policy decision.
Success rates are computed from recorded outcomes only — an unobserved
route reports ``"no evidence"``, never a guess.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["OperationalLedger", "EVENT_KINDS"]

EVENT_KINDS = ("tool", "route", "provider", "task", "prompt", "research")

MAX_EVENTS = 50_000
MAX_DETAIL = 280


class OperationalLedger:
    """Bounded SQLite event ledger with aggregate queries."""

    def __init__(self, db: Any, *, max_events: int = MAX_EVENTS) -> None:
        self._db = db
        self.max_events = max(100, int(max_events))
        db.execute("""
            CREATE TABLE IF NOT EXISTS operational_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                subject TEXT NOT NULL,
                outcome TEXT NOT NULL,          -- success | failure | rejected
                latency_ms REAL NOT NULL DEFAULT 0.0,
                error TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )""")
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ops_subject
            ON operational_events (kind, subject, outcome)""")

    # -- write ------------------------------------------------------------------

    def record(self, kind: str, subject: str, *, outcome: str,
               latency_ms: float = 0.0, error: str = "",
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        kind = (kind or "").strip().lower()
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown operational kind {kind!r}; expected "
                             f"one of {EVENT_KINDS}")
        outcome = (outcome or "").strip().lower()
        if outcome not in ("success", "failure", "rejected"):
            raise ValueError("outcome must be success|failure|rejected")
        subject = (subject or "").strip()[:120]
        if not subject:
            raise ValueError("subject must be non-empty")
        self._db.execute(
            "INSERT INTO operational_events (kind, subject, outcome, "
            "latency_ms, error, context, created_at) VALUES (?, ?, ?, ?, "
            "?, ?, ?)",
            (kind, subject, outcome, max(0.0, float(latency_ms or 0.0)),
             (error or "")[:MAX_DETAIL],
             json.dumps(dict(context or {}), default=str)[:800], time.time()))
        self._prune()
        return {"recorded": True, "kind": kind, "subject": subject,
                "outcome": outcome}

    # -- aggregates ---------------------------------------------------------------

    def stats(self, kind: str = "", *, subject: str = "",
              window: int = 2000) -> Dict[str, Any]:
        """Success rate/latency/error patterns over recorded outcomes."""
        where: List[str] = []
        params: List[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind.strip().lower())
        if subject:
            where.append("subject = ?")
            params.append(subject.strip()[:120])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.query(
            "SELECT kind, subject, COUNT(*) AS total, "
            "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN outcome='rejected' THEN 1 ELSE 0 END) AS denied, "
            "AVG(latency_ms) AS avg_latency, MAX(latency_ms) AS max_latency "
            f"FROM (SELECT * FROM operational_events {clause} "
            "ORDER BY id DESC LIMIT ?) "
            "GROUP BY kind, subject ORDER BY total DESC",
            tuple(params) + (max(10, min(int(window), 20000)),))
        subjects = {}
        errors = self._db.query(
            "SELECT subject, error, COUNT(*) AS c FROM operational_events "
            f"{clause + (' AND ' if clause else 'WHERE ')}outcome='failure' "
            "AND error != '' GROUP BY subject, error ORDER BY c DESC LIMIT 10",
            tuple(params))
        error_patterns = [{"subject": row["subject"],
                           "error": row["error"][:160],
                           "count": int(row["c"])} for row in errors]
        for row in rows:
            total = int(row["total"] or 0)
            ok = int(row["ok"] or 0)
            key = f"{row['kind']}:{row['subject']}"
            subjects[key] = {
                "samples": total,
                "success_rate": round(ok / total, 4) if total else None,
                "established": total >= 5,      # below this: no claim
                "failures": int(row["failed"] or 0),
                "policy_denials": int(row["denied"] or 0),
                "avg_latency_ms": round(float(row["avg_latency"] or 0.0), 1),
                "max_latency_ms": round(float(row["max_latency"] or 0.0), 1),
            }
        return {"subjects": subjects, "error_patterns": error_patterns,
                "note": ("policy denials are counted separately: a denial is "
                         "authorization working, not a defect to route around")}

    def reliability(self, kind: str, subject: str) -> Optional[float]:
        stats = self.stats(kind, subject=subject)["subjects"].get(
            f"{kind}:{subject}")
        if not stats or not stats["established"]:
            return None
        return float(stats["success_rate"] or 0.0)

    # -- bounds ---------------------------------------------------------------------

    def count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS c FROM operational_events")
        return int(row["c"]) if row is not None else 0

    def _prune(self) -> None:
        total = self.count()
        overflow = total - self.max_events
        if overflow > 0:
            self._db.execute(
                "DELETE FROM operational_events WHERE id IN (SELECT id "
                "FROM operational_events ORDER BY id ASC LIMIT ?)", (overflow,))
