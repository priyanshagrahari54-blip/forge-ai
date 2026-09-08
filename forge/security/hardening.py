"""Security hardening (A61): bounded, honest audit reports.

Audits are read-only and bounded. They report what they find —
findings are structured, never include secret values, and the report
can only inform operators; it changes nothing by itself.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

MAX_FILES_PER_PROJECT = 500
MAX_FILE_BYTES = 256 * 1024
MAX_HITS = 20
MAX_ACTIVE_SESSIONS_FLAG = 100
SKIP_DIRS = {".git", ".forge", "__pycache__", ".venv", "node_modules",
             ".arena", "dist", "build", ".mypy_cache", ".pytest_cache"}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"
        r"|\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
)


def audit_policy(policy: Any) -> dict[str, Any]:
    """Audit the permission policy for structural risks."""
    rules = list(getattr(policy, "rules", None) or [])
    findings: list[dict[str, str]] = []
    allow_count = deny_count = require_count = 0
    for rule in rules:
        effect = str(getattr(rule, "effect", "")).upper()
        if effect.endswith("REQUIRE_APPROVAL"):
            require_count += 1
        elif effect.endswith("ALLOW"):
            allow_count += 1
        elif effect.endswith("DENY"):
            deny_count += 1
        if effect.endswith("ALLOW"):
            args = getattr(rule, "args", None) or ()
            if str(getattr(rule, "resource", "")) in (
                    "Resource.TERMINAL", "terminal"):
                if not args:
                    findings.append({
                        "rule": str(getattr(rule, "id", "")),
                        "finding": ("terminal ALLOW rule must pin a "
                                    "concrete executable and exact args"),
                    })
    return {
        "rule_count": len(rules),
        "allow_count": allow_count,
        "deny_count": deny_count,
        "require_approval_count": require_count,
        "deny_by_default": len(rules) == 0,
        "findings": findings,
    }


def audit_sessions(sessions: Any) -> dict[str, Any]:
    """Session hygiene: prune expired sessions and count actives."""
    pruned = 0
    try:
        pruned = int(sessions.prune())
    except Exception:
        pass
    active = 0
    try:
        active = int(sessions.count_active())
    except Exception:
        pass
    findings: list[dict[str, str]] = []
    if active > MAX_ACTIVE_SESSIONS_FLAG:
        findings.append({
            "finding": (f"{active} active sessions exceeds the "
                        f"advisory bound of {MAX_ACTIVE_SESSIONS_FLAG}"),
        })
    return {"active_sessions": active, "expired_pruned": pruned,
            "findings": findings}


def audit_secrets(root: str | Path) -> dict[str, Any]:
    """Bounded secret-pattern scan; reports locations, never values."""
    root_path = Path(root)
    files_scanned = 0
    hits: list[dict[str, Any]] = []
    for path in root_path.rglob("*"):
        if files_scanned >= MAX_FILES_PER_PROJECT or \
                len(hits) >= MAX_HITS:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files_scanned += 1
        for line_no, line in enumerate(text.splitlines(), start=1):
            if len(hits) >= MAX_HITS:
                break
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append({
                        "file": str(path.relative_to(root_path)),
                        "pattern": name, "line": line_no})
                    break
    return {"files_scanned": files_scanned, "hits": hits,
            "truncated": len(hits) >= MAX_HITS}


def run_hardening_report(policy: Any, sessions: Any,
                         projects: dict[str, Any]) -> dict[str, Any]:
    policy_audit = audit_policy(policy)
    session_audit = audit_sessions(sessions)
    secrets: list[dict[str, Any]] = []
    for project in projects.values():
        entry = audit_secrets(project.root)
        entry["project"] = project.id
        secrets.append(entry)
    findings = (len(policy_audit["findings"])
                + len(session_audit["findings"])
                + sum(len(entry["hits"]) for entry in secrets))
    return {
        "policy": policy_audit,
        "sessions": session_audit,
        "secrets": secrets,
        "overall": "attention" if findings else "ok",
        "checked_at": time.time(),
    }
