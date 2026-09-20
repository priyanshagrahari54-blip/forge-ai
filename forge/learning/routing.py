"""Routing learning (A84 Stage I4).

Historical performance may improve *selection order*: model choice,
specialist choice, tool choice and fallback ordering. The mechanism is a
bounded prior — a small additive score adjustment derived from recorded
outcomes — with hard limits that are structural, not advisory:

* adjustments live in ``[-MAX_ADJUSTMENT, +MAX_ADJUSTMENT]`` (default ±0.05)
  inside a 1.0-weighted score scale; they cannot flip a decision driven by
  capability eligibility, health gating, policy or the fallback ladder;
* an adjustment exists only with at least ``MIN_SAMPLES`` recorded outcomes
  for that exact (kind, subject, context) triple — otherwise it is 0.0 and
  the ledger reports "no evidence yet" (Stage R: memory storage is not
  learning; learning requires evidence);
* priors are consulted *after* every hard filter. A prior can prefer a
  model; it can never *un-reject* one that failed capability, verification,
  classification or policy checks — those rejections happen before scoring
  and are not stored here;
* explicit user choices outrank learning: when a request pins a model or
  the policy demands local/free, the prior is simply not consulted.

This module is a store + pure function. Attaching it to the fabric is a
separate, explicit step (:meth:`forge.models.fabric.ModelFabric.attach_learning`)
so no behaviour changes unless an operator opts in.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["RoutingPriors"]

MAX_ADJUSTMENT = 0.05
MIN_SAMPLES = 10
MAX_ROWS = 100_000
#: Latency beyond this ceiling *lowers* a prior regardless of success rate.
LATENCY_PENALTY_MS = 15_000.0

KINDS = ("model", "specialist", "tool", "fallback")


class RoutingPriors:
    """Bounded, evidence-gated priors over routing selections."""

    def __init__(self, db: Any, *, max_adjustment: float = MAX_ADJUSTMENT,
                 min_samples: int = MIN_SAMPLES,
                 now: Optional[Any] = None) -> None:
        self._db = db
        self.max_adjustment = max(0.0, min(float(max_adjustment), 0.10))
        self.min_samples = max(3, int(min_samples))
        self._now = now or time.time
        db.execute("""
            CREATE TABLE IF NOT EXISTS routing_priors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                subject TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL,
                latency_ms REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL
            )""")
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_priors_lookup
            ON routing_priors (kind, subject, context)""")

    # -- evidence collection ---------------------------------------------------

    def record(self, kind: str, subject: str, *, success: bool,
                latency_ms: float = 0.0, context: str = "") -> Dict[str, Any]:
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"unknown prior kind {kind!r}; expected one of "
                             f"{KINDS}")
        subject = (subject or "").strip()[:120]
        if not subject:
            raise ValueError("prior subject must be non-empty")
        self._db.execute(
            "INSERT INTO routing_priors (kind, subject, context, success, "
            "latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, subject, (context or "").strip()[:64],
             1 if success else 0,
             max(0.0, float(latency_ms or 0.0)), self._now()))
        self._prune()
        return self.stats(kind, subject, context=context)

    # -- prior computation -----------------------------------------------------

    def adjustment(self, kind: str, subject: str, *,
                   context: str = "") -> float:
        """The bounded score delta for preferring *subject* (0.0 = no evidence)."""
        stats = self.stats(kind, subject, context=context)
        if stats["samples"] < self.min_samples:
            return 0.0
        rate = stats["success_rate"]
        delta = (rate - 0.5) * 2.0 * self.max_adjustment   # ±max at 0/1
        if stats["avg_latency_ms"] > LATENCY_PENALTY_MS:
            delta -= self.max_adjustment * 0.5
        return max(-self.max_adjustment,
                   min(self.max_adjustment, round(delta, 6)))

    def stats(self, kind: str, subject: str, *,
              context: str = "") -> Dict[str, Any]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total, SUM(success) AS ok, "
            "AVG(latency_ms) AS avg_latency FROM routing_priors "
            "WHERE kind = ? AND subject = ? AND context = ?",
            (kind.strip().lower(), subject.strip()[:120],
             (context or "").strip()[:64]))
        total = int(row["total"] or 0) if row is not None else 0
        ok = int(row["ok"] or 0) if row is not None else 0
        return {
            "kind": kind, "subject": subject, "context": context or "",
            "samples": total,
            "success_rate": round(ok / total, 4) if total else 0.0,
            "avg_latency_ms": round(float(row["avg_latency"] or 0.0), 1)
            if row is not None else 0.0,
            "established": total >= self.min_samples,
            "max_adjustment": self.max_adjustment,
            "note": "0.0 adjustment until established; priors only order "
                    "candidates that already passed every hard filter",
        }

    def ranked_preference(self, kind: str, subjects: List[str], *,
                          context: str = "") -> List[Tuple[str, float]]:
        """(subject, adjustment) pairs sorted for a planner's tie-break list."""
        pairs = [(subject, self.adjustment(kind, subject, context=context))
                 for subject in subjects]
        pairs.sort(key=lambda item: (-item[1], item[0]))
        return pairs

    def report(self, *, limit: int = 50) -> Dict[str, Any]:
        rows = self._db.query(
            "SELECT kind, subject, context, COUNT(*) AS total, SUM(success) "
            "AS ok, AVG(latency_ms) AS avg_latency FROM routing_priors "
            "GROUP BY kind, subject, context ORDER BY total DESC LIMIT ?",
            (max(1, min(int(limit), 500)),))
        entries = []
        for row in rows:
            total = int(row["total"] or 0)
            ok = int(row["ok"] or 0)
            entries.append({
                "kind": row["kind"], "subject": row["subject"],
                "context": row["context"], "samples": total,
                "success_rate": round(ok / total, 4) if total else 0.0,
                "established": total >= self.min_samples,
                "avg_latency_ms": round(float(row["avg_latency"] or 0.0), 1),
            })
        return {"entries": entries, "min_samples": self.min_samples,
                "never_overrides": ["security", "authorization",
                                     "capability requirements",
                                     "explicit user choices"]}

    # -- bounds -------------------------------------------------------------------

    def _prune(self) -> None:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM routing_priors")
        total = int(row["c"]) if row is not None else 0
        overflow = total - MAX_ROWS
        if overflow > 0:
            self._db.execute(
                "DELETE FROM routing_priors WHERE id IN (SELECT id FROM "
                "routing_priors ORDER BY id ASC LIMIT ?)", (overflow,))
