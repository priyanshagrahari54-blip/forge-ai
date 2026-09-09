"""Final commit gate (A75): repository readiness for commits.

Read-only git checks: a repository exists, author identity is
configured, and HEAD exists. The gate reports reasons and never
commits anything itself.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any


def _git(root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        timeout=10,
        check=False)
    return result.stdout.strip()


def commit_gate(plane: Any, session: Any) -> dict[str, Any]:
    project = plane.get_project(session.project_id)
    root = str(project.root)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: str) -> None:
        checks.append({"check": name, "passed": bool(passed),
                       "evidence": evidence})

    try:
        is_repo = _git(root, "rev-parse", "--is-inside-work-tree") \
            == "true"
        check("repository", is_repo,
              "git work tree present" if is_repo else
              "not a git repository")
    except Exception as exc:
        check("repository", False, f"git unavailable: {exc}")
        is_repo = False

    if is_repo:
        try:
            name = _git(root, "config", "user.name")
            email = _git(root, "config", "user.email")
            check("author_identity", bool(name) and bool(email),
                  f"user.name={name or '<unset>'} "
                  f"user.email={email or '<unset>'}")
        except Exception as exc:
            check("author_identity", False, f"git config failed: {exc}")
        try:
            head = _git(root, "rev-parse", "HEAD")
            check("head_exists", bool(head),
                  "HEAD exists" if head else "no commits yet")
        except Exception as exc:
            check("head_exists", False, f"rev-parse failed: {exc}")

    passed = all(entry["passed"] for entry in checks) and is_repo
    return {"passed": bool(passed), "checks": checks,
            "checked_at": time.time(),
            "note": "Read-only readiness checks; the gate never "
                    "commits or pushes."}
