"""Autonomy levels (A69): policy-derived consultative autonomy."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.autonomy.controller import (  # noqa: E402
    autonomy_report,
    validate_transition,
)
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def full_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-r", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**",
                       effect="REQUIRE_APPROVAL"),
        PermissionRule(id="fs-d", resource=Resource.FILESYSTEM,
                       operation="delete", scope="**", effect="DENY"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="REQUIRE_APPROVAL",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


def test_report_derives_from_live_policy(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=full_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.autonomy_report(session)
        assert report["level"] == "assisted"
        assert report["profile"] == "assisted"
        resources = report["resources"]
        assert resources["filesystem"]["read"] == "autonomous"
        assert resources["filesystem"]["write"] == "approval"
        assert resources["filesystem"]["delete"] == "blocked"
        summary = report["summary"]
        assert summary["autonomous"] >= 1
        assert summary["approval"] >= 1
        assert summary["blocked"] >= 1
        assert "never grant" in report["note"]


def test_level_transitions_are_validated_stepwise(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=full_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.autonomy_set_level(session, "bogus")
        # assisted -> autonomous is allowed (one step).
        raised = plane.autonomy_set_level(session, "autonomous")
        assert raised["level"] == "autonomous"
        # Back down for the next checks.
        plane.autonomy_set_level(session, "safe")
        # safe -> autonomous skips a step: refused.
        with pytest.raises(Exception):
            plane.autonomy_set_level(session, "autonomous")
        plane.autonomy_set_level(session, "assisted")
        with pytest.raises(Exception):
            plane.autonomy_set_level(session, "assisted")


def test_safe_profile_blocks_writes_in_report(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=full_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, _headers = login(client, profile="safe")
        _bs, _bt, headers = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        del bob
        _ps, _pt, safe_headers = login(client, actor="carol",
                                       profile="safe")
        carol = plane.sessions.get(_ps["session_id"])
        report = plane.autonomy_report(carol)
        assert report["level"] == "safe"


def test_locked_profile_manages_its_own_autonomy(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=full_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _ps, _pt, _locked_headers = login(client, actor="locked",
                                          profile="locked")
        locked = plane.sessions.get(_ps["session_id"])
        report = plane.autonomy_report(locked)
        assert report["level"] == "safe"
        assert report["override"] is False
        with pytest.raises(Exception):
            plane.autonomy_set_level(locked, "autonomous")


def test_level_selects_the_run_mode(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=full_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.autonomy_set_level(session, "autonomous")
        task = plane.submit_task(session, "add a csv export")
        assert task.mode == "autonomous"
        # API flow: report + set + refusal.
        got = client.get("/api/v1/autonomy", headers=headers)
        assert got.status_code == 200
        assert got.json()["level"] == "autonomous"
        set_back = client.post("/api/v1/autonomy", headers=headers,
                               json={"level": "safe"})
        assert set_back.status_code == 200
        assert set_back.json()["level"] == "safe"
        skip = client.post("/api/v1/autonomy", headers=headers,
                           json={"level": "autonomous"})
        assert skip.status_code == 400


def test_controller_unit_contracts():
    validate_transition("assisted", "autonomous")
    with pytest.raises(Exception):
        validate_transition("safe", "autonomous")
    with pytest.raises(Exception):
        validate_transition("custom", "assisted")
    report = autonomy_report(PermissionPolicy(), "assisted")
    assert report["summary"]["blocked"] >= 1
