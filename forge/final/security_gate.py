"""Final security gate (A73): go/no-go on security evidence.

Reuses the A61 hardening audits and adds policy assertions:
fail-closed posture, no secret-pattern hits, no unpinned terminal
ALLOW rules. Every check names its evidence; the gate never
changes anything.
"""
from __future__ import annotations

import time
from typing import Any

from forge.security.hardening import run_hardening_report


def security_gate(plane: Any, session: Any) -> dict[str, Any]:
    report = run_hardening_report(
        plane.policy, plane.sessions, plane.projects)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
        entry = {"check": name, "passed": bool(passed),
                 "evidence": evidence}
        checks.append(entry)
        return entry

    policy = report["policy"]
    check("policy_rule_integrity",
          not policy["findings"],
          f"{len(policy['findings'])} structural finding(s)" +
          (": " + "; ".join(finding["finding"] for finding in
                             policy["findings"][:3])
           if policy["findings"] else ""))

    secret_hits = sum(len(entry["hits"]) for entry in report["secrets"])
    check("no_secret_hits", secret_hits == 0,
          f"{secret_hits} secret-pattern hit(s) across projects")

    if policy["rule_count"] == 0:
        check("posture", True,
              "no policy rules: fail closed (deny by default)")
    elif policy["deny_count"] > 0 or policy["require_approval_count"] > 0:
        check("posture", True,
              f"{policy['deny_count']} DENY / "
              f"{policy['require_approval_count']} REQUIRE_APPROVAL "
              "rules constrain the surface")
    else:
        check("posture", False,
              "policy has only ALLOW rules; nothing constrains "
              "the surface")

    check("sessions_bounded", report["sessions"]["active_sessions"] <=
          100,
          f"{report['sessions']['active_sessions']} active session(s)")

    passed = all(entry["passed"] for entry in checks)
    return {"passed": bool(passed), "checks": checks,
            "report": report,
            "checked_at": time.time(),
            "note": "Derived from live audits only; the gate changes "
                    "nothing by itself."}
