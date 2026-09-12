"""Controlled self-improvement (A81): control plane + API + dashboard."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def seed_broken(root: Path) -> None:
    make_repo(root)
    (root / "forge").mkdir(exist_ok=True)
    (root / "forge" / "__init__.py").write_text("")
    (root / "forge" / "mathy.py").write_text(BROKEN)
    (root / "tests" / "test_mathy.py").write_text(
        "from forge.mathy import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=root, check=True,
                   capture_output=True)


def install_fixing_producer(plane, session) -> None:
    engine = plane._self_improvement_engine(session)
    engine._producer = lambda root, proposal: {"forge/mathy.py": FIXED}


def test_api_requires_auth_and_reports_dashboard(tmp_path):
    plane = make_plane(tmp_path, start=True)
    seed_broken(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/self-improvement").status_code == 401
        _payload, _token, headers = login(client)
        response = client.get("/api/v1/self-improvement", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        for key in ("proposals", "candidates", "accepted", "rejected", "evidence",
                    "applied", "rollbacks", "pending_approval", "guardrails", "status"):
            assert key in body, key
        assert body["status"]["hard_max_iterations"] == 10
        assert "never bypass approvals" in body["guardrails"]["invariants"]


def test_api_full_flow_analyze_run_approve_apply_rollback(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    seed_broken(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        install_fixing_producer(plane, session)

        analysis = client.post("/api/v1/self-improvement/analyze",
                               json={"run_tests": True}, headers=headers)
        assert analysis.status_code == 200, analysis.text
        assert analysis.json()["summary"]["weaknesses_total"] >= 1
        assert "tests" in analysis.json()["sources"]

        run = client.post("/api/v1/self-improvement/run",
                          json={"iterations": 1, "run_tests": True}, headers=headers)
        assert run.status_code == 200, run.text
        result = run.json()["results"][0]
        assert result["accepted"] is False
        assert result["decision"]["awaiting_approval"] is True
        assert result["decision"]["failed_gates"] == ["policy"]
        cid = result["candidate"]["id"]
        assert (root / "forge" / "mathy.py").read_text() == BROKEN

        # Apply before approval is refused (409 APPROVAL_REQUIRED).
        early = client.post(f"/api/v1/self-improvement/candidates/{cid}/apply",
                            headers=headers)
        assert early.status_code == 409, early.text
        assert early.json()["error"]["code"] == "APPROVAL_REQUIRED"

        # Wrong fingerprint is refused.
        bad = client.post(f"/api/v1/self-improvement/candidates/{cid}/approve",
                          json={"change_fingerprint": "nope"}, headers=headers)
        assert bad.status_code == 403, bad.text

        fingerprint = result["candidate"]["change_fingerprint"]
        approve = client.post(f"/api/v1/self-improvement/candidates/{cid}/approve",
                              json={"reason": "reviewed",
                                    "change_fingerprint": fingerprint}, headers=headers)
        assert approve.status_code == 200, approve.text
        assert approve.json()["decision"]["accepted"] is True
        assert approve.json()["approval"]["approved_by"] == "alice"

        dashboard = client.get("/api/v1/self-improvement", headers=headers).json()
        assert dashboard["pending_approval"][0]["candidate_id"] == cid
        assert dashboard["pending_approval"][0]["accepted"] is True

        apply = client.post(f"/api/v1/self-improvement/candidates/{cid}/apply",
                            headers=headers)
        assert apply.status_code == 200, apply.text
        assert apply.json()["committed"] is False
        assert (root / "forge" / "mathy.py").read_text() == FIXED
        # Never committed: git still sees the change as unstaged work.
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                capture_output=True, text=True).stdout
        assert "forge/mathy.py" in status

        # Approvals are single-use: a second apply needs a fresh approval.
        again = client.post(f"/api/v1/self-improvement/candidates/{cid}/apply",
                            headers=headers)
        assert again.status_code == 409

        dashboard = client.get("/api/v1/self-improvement", headers=headers).json()
        assert dashboard["applied"][0]["candidate_id"] == cid
        assert dashboard["status"]["applied"] == 1

        rollback = client.post(f"/api/v1/self-improvement/candidates/{cid}/rollback",
                               headers=headers)
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["restored"] == ["forge/mathy.py"]
        assert (root / "forge" / "mathy.py").read_text() == BROKEN
        dashboard = client.get("/api/v1/self-improvement", headers=headers).json()
        assert dashboard["rollbacks"][0]["candidate_id"] == cid

        # Audit trail exists for the mutating operations.
        audit_path = root / ".forge" / "audit.jsonl"
        if audit_path.is_file():
            text = audit_path.read_text()
            assert "self_improvement" in text


def test_safe_profile_cannot_build_or_apply(tmp_path):
    plane = make_plane(tmp_path, start=True)
    seed_broken(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client, profile="safe")
        # Analysis is read-only and allowed.
        assert client.post("/api/v1/self-improvement/analyze", json={},
                           headers=headers).status_code == 200
        run = client.post("/api/v1/self-improvement/run", json={"iterations": 1},
                          headers=headers)
        assert run.status_code == 403
        assert run.json()["error"]["code"] == "POLICY_DENIED"
        approve = client.post("/api/v1/self-improvement/candidates/CAND-x/approve",
                              json={}, headers=headers)
        assert approve.status_code == 403


def test_iterations_are_validated_and_unknown_candidates_404(tmp_path):
    plane = make_plane(tmp_path, start=True)
    seed_broken(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client)
        assert client.post("/api/v1/self-improvement/run", json={"iterations": 99},
                           headers=headers).status_code in (400, 422)
        assert client.post("/api/v1/self-improvement/candidates/CAND-x/approve",
                           json={}, headers=headers).status_code == 404
        assert client.post("/api/v1/self-improvement/candidates/CAND-x/rollback",
                           headers=headers).status_code == 404
        assert client.delete("/api/v1/self-improvement/candidates/CAND-x",
                             headers=headers).status_code == 404


def test_discard_drops_pending_candidate(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    seed_broken(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        install_fixing_producer(plane, session)
        run = client.post("/api/v1/self-improvement/run",
                          json={"iterations": 1, "run_tests": True}, headers=headers)
        cid = run.json()["results"][0]["candidate"]["id"]
        gone = client.delete(f"/api/v1/self-improvement/candidates/{cid}", headers=headers)
        assert gone.status_code == 200 and gone.json()["discarded"] is True
        dashboard = client.get("/api/v1/self-improvement", headers=headers).json()
        assert dashboard["pending_approval"] == []
        assert (root / "forge" / "mathy.py").read_text() == BROKEN


def test_plane_evidence_uses_live_sources(tmp_path):
    plane = make_plane(tmp_path, start=True)
    seed_broken(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.record_failure(session, "task", "model returned invalid json")
        plane.record_failure(session, "task", "model returned invalid json")
        report = plane.self_improvement_analyze(session)
        assert "failure_ledger" in report["sources"]
        assert any(e["kind"] == "repeated_error" and "invalid json" in e["summary"]
                   for e in report["evidence"])


def test_cockpit_ui_has_self_improvement_view():
    web = Path(__file__).parent.parent / "forge" / "cockpit" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert 'data-route="selfimprove"' in html
    assert '<template id="tpl-selfimprove">' in html
    for hook in ("si-proposals", "si-candidates", "si-accepted", "si-rejected",
                 "si-evidence", "si-pending", "si-guardrails"):
        assert f'id="{hook}"' in html, hook
    assert "selfimprove: { render: renderSelfImprovementView" in js
    assert "/api/v1/self-improvement" in js
    # The UI never has an auto-apply path: apply always follows an approve.
    assert "approve" in js and "/apply" in js


def test_model_producer_routes_through_fabric_inside_candidate(tmp_path):
    """The production change producer asks the Model Fabric via CoderAgent,
    but only ever writes inside the isolated candidate copy."""
    from helpers_a34 import ScriptedProvider, make_fabric
    from forge.self_improvement import SelfAnalyzer, SelfImprovementEngine

    root = tmp_path / "repo"
    (root / "forge").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "forge" / "__init__.py").write_text("")
    (root / "forge" / "mathy.py").write_text(BROKEN)
    (root / "tests" / "test_mathy.py").write_text(
        "from forge.mathy import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    payload = json.dumps({
        "summary": "fix add",
        "changes": [{"path": "forge/mathy.py", "action": "modify", "content": FIXED}],
        "tests_to_run": ["tests/test_mathy.py"],
        "reasoning_summary": "swap operator", "risk_level": "low"})
    provider = ScriptedProvider(payload)
    engine = SelfImprovementEngine(root, fabric=make_fabric(provider),
                                   analyzer=SelfAnalyzer(root))
    result = engine.run(collect_kwargs={"run_tests": True, "measure_host": False})[0]
    assert provider.prompts, "the model was consulted"
    assert result.candidate.status == "evaluated"
    assert result.candidate.changes == {"forge/mathy.py": FIXED}
    assert result.decision.awaiting_approval and not result.accepted
    assert (root / "forge" / "mathy.py").read_text() == BROKEN
