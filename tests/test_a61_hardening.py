"""Security hardening (A61): bounded read-only audit reports."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.security.hardening import audit_policy  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def clean_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="ALLOW", args=("/usr/bin/python", "-m",
                                             "pytest")),
        PermissionRule(id="deny-net", resource=Resource.NETWORK,
                       operation="request", scope="example.com",
                       effect="DENY"),
    ])


def test_clean_plane_reports_ok(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=clean_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.hardening_report(session)
        assert report["overall"] == "ok"
        assert report["policy"]["rule_count"] == 3
        assert report["policy"]["allow_count"] == 2
        assert report["policy"]["deny_count"] == 1
        assert report["policy"]["findings"] == []
        assert report["sessions"]["active_sessions"] >= 1
        assert report["sessions"]["expired_pruned"] >= 0
        scanned = sum(entry["files_scanned"]
                      for entry in report["secrets"])
        assert scanned >= 1
        assert all(entry["hits"] == [] for entry in report["secrets"])


def test_planted_secrets_are_flagged_without_values(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=clean_policy())
    make_repo(Path(plane.projects["demo"].root))
    (Path(plane.projects["demo"].root) / "leak.txt").write_text(
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
        "-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8")
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.hardening_report(session)
        assert report["overall"] == "attention"
        hits = [hit for entry in report["secrets"]
                for hit in entry["hits"]]
        patterns = {hit["pattern"] for hit in hits}
        assert "github-token" in patterns
        assert "private-key" in patterns
        # Reports locations, never values.
        assert all("ghp_" not in hit["file"] for hit in hits)
        assert all("pattern" in hit and "line" in hit
                   for hit in hits)
        leaked = report
        assert "ghp_abcdefg" not in repr(leaked)


def test_policy_audit_flags_unpinned_terminal_rules():
    # Defense in depth: even a hand-built rule object that bypassed
    # constructor validation is flagged by the audit.
    fabricated = SimpleNamespace(rules=[SimpleNamespace(
        id="bad-term", resource="terminal", operation="execute",
        scope="src", effect="ALLOW", args=())])
    audit = audit_policy(fabricated)
    assert any("terminal ALLOW" in finding["finding"]
               for finding in audit["findings"])
    assert audit_policy(clean_policy())["findings"] == []


def test_deny_by_default_is_reported(tmp_path):
    plane = make_plane(tmp_path, start=True)  # empty policy
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.hardening_report(session)
        assert report["policy"]["deny_by_default"] is True


def test_hardening_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=clean_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.get("/api/v1/hardening/report",
                              headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"policy", "sessions", "secrets", "overall"}
        assert body["overall"] in ("ok", "attention")
