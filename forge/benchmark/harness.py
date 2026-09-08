"""Model benchmarking (A60): real checks judged by code, never by models.

Every check sends a bounded prompt through the fabric and is judged
by deterministic code on the actual response. Results are stored and
reported honestly — including failures. No self-reported capability
claims are accepted.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from forge.models.request import ModelRequest

MAX_MODELS = 5
MAX_CHECKS = 8
CHECK_TIMEOUT = 30.0
CAPABILITY = "coding"


def _judge_json_object(text: str) -> bool:
    try:
        data = json.loads(text.strip().strip("`"))
    except (ValueError, TypeError):
        return False
    return (isinstance(data, dict)
            and data.get("benchmark") is True
            and data.get("value") == 42)


def _judge_arithmetic(text: str) -> bool:
    return re.search(r"\b42\b", text) is not None


def _judge_marker(text: str) -> bool:
    return "FORGE-BENCHMARK-OK" in text


CHECKS: tuple[dict[str, Any], ...] = (
    {"name": "json-object",
     "description": "Strict JSON object with expected fields",
     "prompt": ("Reply with ONLY a JSON object, no prose, with exactly "
                'two keys: "benchmark": true and "value": 42.'),
     "judge": _judge_json_object},
    {"name": "arithmetic",
     "description": "17 + 25 answered with the integer 42",
     "prompt": "What is 17 + 25? Reply with only the integer.",
     "judge": _judge_arithmetic},
    {"name": "marker-echo",
     "description": "Replies containing the exact marker token",
     "prompt": ("Reply with exactly the text FORGE-BENCHMARK-OK and "
                "nothing else."),
     "judge": _judge_marker},
)


def _judged_checks() -> list[dict[str, Any]]:
    return [{"name": check["name"], "description": check["description"],
             "prompt": check["prompt"], "judge": check["judge"]}
            for check in CHECKS]


def run_benchmark(fabric: Any, model_names: list[str] | None = None,
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the suite against real fabric models; judge every answer."""
    registry = fabric.registry
    available = registry.names()
    selected = [name for name in (model_names or available)
                if name in available][:MAX_MODELS]
    unknown = [name for name in (model_names or [])
               if name not in available]
    checks = _judged_checks()[:MAX_CHECKS]
    results: list[dict[str, Any]] = []
    for model_name in selected:
        model = registry.get(model_name)
        started = time.time()
        check_results: list[dict[str, Any]] = []
        for check in checks:
            request = ModelRequest(
                prompt=check["prompt"], capability=CAPABILITY,
                task=f"benchmark:{check['name']}", prefer_local=True,
                prefer_free=True, max_output_tokens=512)
            attempt = time.time()
            try:
                response = fabric.generate(request)
                passed = (response.success
                          and check["judge"](response.text))
                check_results.append({
                    "check": check["name"], "passed": bool(passed),
                    "latency_ms": round(response.latency_ms, 1),
                    "error": "" if response.success else
                    response.error[:200]})
            except Exception as exc:
                check_results.append({
                    "check": check["name"], "passed": False,
                    "latency_ms": round((time.time() - attempt) * 1000, 1),
                    "error": str(exc)[:200]})
        passed = sum(1 for entry in check_results if entry["passed"])
        results.append({
            "model": model_name, "provider": model.provider,
            "passed": passed, "total": len(check_results),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "checks": check_results})
    summary = {
        "models_benchmarked": len(results),
        "checks_per_model": len(checks),
        "unknown_models": unknown,
        "passed": sum(entry["passed"] for entry in results),
        "total": sum(entry["total"] for entry in results),
        "honest": True,
    }
    return results, summary


class BenchmarkStore:
    """SQLite-backed benchmark history."""

    def __init__(self, db: Any) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS benchmark_runs (
                id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                passed INTEGER NOT NULL,
                total INTEGER NOT NULL,
                elapsed_ms REAL NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )

    def record(self, entry: dict[str, Any], actor: str) -> dict[str, Any]:
        import uuid

        run_id = uuid.uuid4().hex[:12]
        created_at = time.time()
        self._db.execute(
            "INSERT INTO benchmark_runs (id, model, provider, "
            "checks_json, passed, total, elapsed_ms, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, entry["model"], entry["provider"],
             json.dumps(entry["checks"]), entry["passed"],
             entry["total"], entry["elapsed_ms"], actor, created_at))
        row = {"id": run_id, **entry, "actor": actor,
               "created_at": created_at}
        return row

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        rows = self._db.query(
            "SELECT id, model, provider, checks_json, passed, total, "
            "elapsed_ms, actor, created_at FROM benchmark_runs "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,))
        return [{"id": row["id"], "model": row["model"],
                 "provider": row["provider"],
                 "checks": json.loads(row["checks_json"]),
                 "passed": row["passed"], "total": row["total"],
                 "elapsed_ms": row["elapsed_ms"],
                 "actor": row["actor"],
                 "created_at": row["created_at"]} for row in rows]

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, model, provider, checks_json, passed, total, "
            "elapsed_ms, actor, created_at FROM benchmark_runs "
            "WHERE id = ?", (run_id,))
        if row is None:
            return None
        return {"id": row["id"], "model": row["model"],
                "provider": row["provider"],
                "checks": json.loads(row["checks_json"]),
                "passed": row["passed"], "total": row["total"],
                "elapsed_ms": row["elapsed_ms"],
                "actor": row["actor"],
                "created_at": row["created_at"]}
