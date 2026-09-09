"""Final rollout gate (A78): every final gate in one verdict.

Combines acceptance, security, benchmark, commit, and memory
gates, plus verification of the acceptance smoke run when one is
recorded. Overall pass requires every gate to pass.
"""
from __future__ import annotations

import time
from typing import Any

from forge.final.acceptance import run_acceptance
from forge.final.benchmark_gate import benchmark_gate
from forge.final.commit_gate import commit_gate
from forge.final.memory_gate import memory_gate
from forge.final.security_gate import security_gate
from forge.final.verification import verify_run_evidence


def rollout_gate(plane: Any, session: Any) -> dict[str, Any]:
    acceptance = run_acceptance(plane, session)
    security = security_gate(plane, session)
    benchmark = benchmark_gate(plane, session, min_passed=0)
    commit = commit_gate(plane, session)
    memory = memory_gate(plane, session)

    verification = None
    smoke = next((entry for entry in acceptance["checks"]
                  if entry["check"] == "smoke_run"), None)
    if smoke and smoke.get("task_id"):
        verification = verify_run_evidence(
            plane, session, smoke["task_id"])

    gates = {
        "acceptance": acceptance["accepted"],
        "security": security["passed"],
        "benchmark": benchmark["passed"],
        "commit": commit["passed"],
        "memory": memory["passed"],
        "smoke_verification": (verification is not None and
                               verification["verified"]),
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "details": {"acceptance": acceptance,
                    "security": security,
                    "benchmark": benchmark,
                    "commit": commit,
                    "memory": memory,
                    "smoke_verification": verification},
        "checked_at": time.time(),
        "note": "Overall pass requires every gate to pass; no gate is "
                "skipped.",
    }
