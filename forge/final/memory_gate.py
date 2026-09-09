"""Final memory gate (A76): durable memory and learning are live.

Read-only operational checks: the learning ledger answers queries
and the per-agent memory table exists in the plane database.
Nothing is written by the gate.
"""
from __future__ import annotations

import time
from typing import Any


def memory_gate(plane: Any, session: Any) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: str) -> None:
        checks.append({"check": name, "passed": bool(passed),
                       "evidence": evidence})

    try:
        stats = plane._failure_ledger().stats()
        check("learning_ledger", True,
              f"{stats.get('distinct_keys', 0)} distinct failure "
              f"key(s), {stats.get('total_events', 0)} event(s)")
    except Exception as exc:
        check("learning_ledger", False, f"ledger query failed: {exc}")

    try:
        row = plane._db.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='session_memory'")
        check("session_memory_table", row is not None,
              "session_memory table present" if row is not None else
              "session_memory table missing")
    except Exception as exc:
        check("session_memory_table", False,
              f"schema query failed: {exc}")

    passed = all(entry["passed"] for entry in checks)
    return {"passed": bool(passed), "checks": checks,
            "checked_at": time.time(),
            "note": "Operational presence checks only; the gate never "
                    "writes memory."}
