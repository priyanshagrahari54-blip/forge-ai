"""Compute (A48): real local execution, honest quotas, policy gating."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.compute.engine import ComputeEngine  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"


def compute_policy(effect: str = "ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="c-run", resource=Resource.TERMINAL,
                       operation="execute", scope="python",
                       effect=effect),
    ])


def test_engine_executes_real_code(tmp_path):
    engine = ComputeEngine(tmp_path)
    cell = engine.execute("print(sum(range(100)))")
    assert cell["status"] == "succeeded"
    assert cell["return_code"] == 0
    assert "4950" in cell["output"]
    assert cell["backend"] == "local-python"
    assert cell["quota"]["cells_used"] == 1


def test_engine_reports_real_failure(tmp_path):
    engine = ComputeEngine(tmp_path)
    cell = engine.execute("raise ValueError('boom')")
    assert cell["status"] == "failed"
    assert cell["return_code"] != 0
    assert "boom" in cell["output"]


def test_engine_enforces_timeout(tmp_path):
    engine = ComputeEngine(tmp_path, cell_timeout=1.0)
    cell = engine.execute("import time; time.sleep(30)", timeout=1.0)
    assert cell["status"] == "timeout"
    assert cell["timed_out"] is True
    assert cell["return_code"] == -1


def test_engine_enforces_quotas_before_execution(tmp_path):
    engine = ComputeEngine(tmp_path, max_cells=1, max_seconds=60)
    assert engine.execute("print('ok')")["status"] == "succeeded"
    refused = engine.execute("print('again')")
    assert refused["status"] == "refused"
    assert "quota" in refused["reason"]
    assert refused["cell_id"] == ""
    assert engine.quota.cells_used == 1  # refusal consumes nothing


def test_engine_truncates_output_and_bounds_history(tmp_path):
    engine = ComputeEngine(tmp_path, max_cells=3)
    engine.execute("print('x' * 10000)")
    cell = engine.cells[-1]
    assert len(cell["output"]) <= 4000
    assert cell["output_truncated"] is True
    for _ in range(5):
        engine.execute("print('fill')")
    assert len(engine.history()) == 3


def test_plane_compute_gated_and_audited(tmp_path):
    # ALLOW terminal rules pin exact args (A33 hardening), so the
    # policy allows this one exact cell.
    policy = PermissionPolicy(rules=[
        PermissionRule(id="c-run", resource=Resource.TERMINAL,
                       operation="execute", scope="python",
                       args=("-c", "print(6 * 7)"), effect="ALLOW"),
    ])
    plane = make_plane(tmp_path, start=True, policy=policy)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.compute_execute(session, "print(6 * 7)")
        assert result["allowed"] is True
        assert result["cell"]["status"] == "succeeded"
        assert "42" in result["cell"]["output"]
        audited = plane.audit.query(resource="compute")
        assert audited and audited[-1].decision.value == "ALLOW"
        assert "status=succeeded" in audited[-1].reason
        status = plane.compute_status(session)
        assert status["backend"] == "local-python"
        assert "remote or GPU" in status["note"]
        assert plane.compute_history(session)["cells"]


def test_plane_compute_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=compute_policy("DENY"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.compute_execute(session, "print('never')")
        assert result["allowed"] is False
        assert result["cell"] is None
        assert plane.compute_history(session)["cells"] == []


def test_plane_compute_approval_round_trip(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=compute_policy("REQUIRE_APPROVAL"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        first = plane.compute_execute(session, "print(1)")
        assert first["allowed"] is False
        approval_id = first["approval_request_id"]
        assert any(item["id"] == approval_id
                   for item in plane.list_compute_approvals(session))
        decided = plane.decide_compute_approval(session, approval_id, True)
        second = plane.compute_execute(
            session, "print(1)", approval_id=decided["token_id"])
        assert second["allowed"] is True
        assert second["cell"]["status"] == "succeeded"
        replay = plane.compute_execute(
            session, "print(2)", approval_id=decided["token_id"])
        assert replay["allowed"] is False
        assert replay["approval_request_id"] != approval_id


def test_compute_api(tmp_path):
    policy = PermissionPolicy(rules=[
        PermissionRule(id="c-run", resource=Resource.TERMINAL,
                       operation="execute", scope="python",
                       args=("-c", "print('hello compute')"),
                       effect="ALLOW"),
    ])
    plane = make_plane(tmp_path, start=True, policy=policy)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/compute/status").status_code == 401
        _session, _token, headers = login(client)
        run = client.post("/api/v1/compute/execute", headers=headers,
                          json={"code": "print('hello compute')"})
        assert run.status_code == 200
        assert run.json()["allowed"] is True
        assert "hello compute" in run.json()["cell"]["output"]
        assert client.post("/api/v1/compute/execute", headers=headers,
                           json={"code": ""}).status_code == 400
        status = client.get("/api/v1/compute/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["quota"]["cells_used"] == 1
        history = client.get("/api/v1/compute/history", headers=headers)
        assert history.status_code == 200
        assert history.json()["cells"]


def test_cockpit_compute_view_contracts():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    assert '<template id="tpl-compute">' in html
    for hook in ("compute-input", "compute-run", "compute-result",
                 "compute-quota", "compute-history"):
        assert f'id="{hook}"' in html, hook
    assert 'href="#/compute"' in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "compute" in routes_block
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Compute" in palette
    renderer = js.split("function renderComputeView", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls and set(calls) == {"/api/v1/compute/execute",
                                    "/api/v1/compute/status",
                                    "/api/v1/compute/history"}, calls
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
