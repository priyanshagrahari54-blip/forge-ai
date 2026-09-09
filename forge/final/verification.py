"""Final verification (A72): evidence checks on a finished run.

Verification never re-executes anything: it inspects the run
record (terminal status, report presence) and checks that every
recorded output file exists on disk. Anything missing fails the
gate with the honest reason.
"""
from __future__ import annotations

import json
from typing import Any


def verify_run_evidence(plane: Any, session: Any, run_id: str
                        ) -> dict[str, Any]:
    run = plane.runs.get(run_id)
    if run is None or run.project_id != session.project_id:
        return {"run_id": run_id, "verified": False,
                "status": "NOT_FOUND",
                "checks": [], "reason": "unknown task in this project"}
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
        entry = {"check": name, "passed": bool(passed),
                 "evidence": evidence}
        checks.append(entry)
        return entry

    raw_status = getattr(run.status, "value", str(run.status))
    check("terminal_status", raw_status in
          ("SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"),
          f"status {raw_status}")
    check("succeeded", raw_status == "SUCCEEDED",
          f"status {raw_status}")

    report = run.report()
    check("report_recorded", isinstance(report, dict) and bool(report),
          "report_json present" if isinstance(report, dict)
          and bool(report) else "report_json missing or empty")

    from pathlib import Path as _Path

    files = run.files()
    project_root = _Path(plane.get_project(run.project_id).root)
    missing = [path for path in files
               if not (project_root / path).is_file()]
    check("files_exist", files and not missing,
          f"{len(files) - len(missing)}/{len(files)} recorded files "
          "on disk" if files else "no files recorded")

    verified = raw_status == "SUCCEEDED" and \
        all(entry["passed"] for entry in checks if
            entry["check"] in ("report_recorded", "files_exist"))
    return {"run_id": run_id, "status": raw_status,
            "verified": bool(verified), "checks": checks,
            "error": run.error[:300] if run.error else "",
            "reason": "" if verified else (
                run.error[:200] if run.error else
                "run evidence is incomplete")}
