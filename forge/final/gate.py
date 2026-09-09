"""Final go/no-go gate (A80): the last word.

Go requires the full rollout to pass AND at least one real
SUCCEEDED run recorded in the project — the end-to-end
demonstration the master build demands. Every requirement is
listed with its evidence.
"""
from __future__ import annotations

import time
from typing import Any

from forge.final.rollout import rollout_gate

TERMINAL_OK = ("SUCCEEDED",)


def final_gate(plane: Any, session: Any) -> dict[str, Any]:
    rollout = rollout_gate(plane, session)
    succeeded_runs = 0
    rows, _total = plane.runs.list_for_project(session.project_id)
    for run in rows:
        raw_status = getattr(run.status, "value", str(run.status))
        if str(raw_status) in TERMINAL_OK:
            succeeded_runs += 1

    requirements = {
        "rollout_passed": rollout["passed"],
        "real_successful_run": succeeded_runs >= 1,
    }
    go = all(requirements.values())
    return {
        "go": bool(go),
        "requirements": requirements,
        "evidence": {
            "rollout": rollout,
            "succeeded_runs": succeeded_runs,
        },
        "checked_at": time.time(),
        "note": ("Go requires every rollout gate to pass and at least "
                 "one genuinely SUCCEEDED run on record; there is no "
                 "shortcut."),
    }
