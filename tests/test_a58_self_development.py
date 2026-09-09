"""Agent self-development (A58): honest learning from real failures."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.models.provider import ModelResult  # noqa: E402
from helpers_a34 import (ScriptedProvider, login, make_client,  # noqa: E402
                         make_plane, make_repo)

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def run_policy(fs_effect: str = "ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect=fs_effect),
    ])


class BadJsonProvider(ScriptedProvider):
    """Deterministic provider that answers with invalid JSON."""

    name = "bad-json"

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ModelResult("not valid json", self.name)


def wait_failed(plane, session, name, run_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = plane.agent_run_result(session, name, run_id)
        if result["status"] == "failed":
            return {"error": result.get("error", "")}
        if result["status"] == "finished":
            run = result["run"]
            if not run["success"]:
                return {"error": run["error"]}
            raise AssertionError("run unexpectedly succeeded")
        time.sleep(0.05)
    raise AssertionError("run never failed")


def test_analyze_without_failures_says_none(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        analyzed = plane.selfdev_analyze(session, "scout")
        assert analyzed["failures_seen"] == 0
        assert analyzed["proposal"]["kind"] == "none"
        with pytest.raises(Exception):
            plane.selfdev_apply(session, "scout",
                                analyzed["proposal"]["proposal_id"])


def test_unbound_failure_is_not_self_fixable(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "spec-only", "coding", ["coding"])
        run = plane.agent_run(session, "spec-only", "write something")
        assert run["allowed"] is True
        failed = wait_failed(plane, session, "spec-only", run["run_id"])
        assert "executor" in failed["error"].lower()
        runs = plane.agent_runs(session, "spec-only")["runs"]
        assert any(not entry["success"] for entry in runs)
        analyzed = plane.selfdev_analyze(session, "spec-only")
        assert analyzed["failures_seen"] == 1
        assert analyzed["proposal"]["kind"] == "none"
        assert "not self-fixable" in analyzed["proposal"]["reason"]
        with pytest.raises(Exception):
            plane.selfdev_apply(session, "spec-only",
                                analyzed["proposal"]["proposal_id"])
        # Nothing changed.
        definition = plane.agent_definitions(
            session)["agents"][0]
        assert definition["generation"] == 1


def test_failure_note_apply_through_validation(tmp_path):
    # The coding executor fails for real (model returned invalid
    # JSON); that recorded failure is what the agent learns from.
    plane = make_plane(tmp_path, start=True,
                       provider=BadJsonProvider(), policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.agent_create(session, "coder", "coding",
                                     ["coding"], bind=True)
        assert created["generation"] == 1
        run = plane.agent_run(session, "coder", "write the csv export")
        wait_failed(plane, session, "coder", run["run_id"])
        analyzed = plane.selfdev_analyze(session, "coder")
        assert analyzed["failures_seen"] >= 1
        proposal = analyzed["proposal"]
        assert proposal["kind"] == "failure-note"
        assert "invalid json" in proposal["note"].lower()
        applied = plane.selfdev_apply(session, "coder",
                                      proposal["proposal_id"])
        assert "[learned]" in applied["agent"]["description"]
        assert "invalid json" in applied["agent"]["description"].lower()
        assert applied["agent"]["generation"] == 2
        assert applied["agent"]["metrics"]["failures"] >= 1
        # Idempotence refusals.
        with pytest.raises(Exception):
            plane.selfdev_apply(session, "coder",
                                proposal["proposal_id"])
        # Ledger keeps the decision.
        entries = plane.selfdev_ledger(session, "coder")["entries"]
        assert len(entries) == 1
        assert entries[0]["applied"] is True
        assert entries[0]["kind"] == "failure-note"


def test_selfdev_budget_caps_learning_loops(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       provider=BadJsonProvider(), policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "coder", "coding", ["coding"],
                           bind=True)
        for _ in range(3):
            run = plane.agent_run(session, "coder",
                                  "write the csv export")
            wait_failed(plane, session, "coder", run["run_id"])
            analyzed = plane.selfdev_analyze(session, "coder")
            plane.selfdev_apply(
                session, "coder",
                analyzed["proposal"]["proposal_id"])
        run = plane.agent_run(session, "coder", "write the csv export")
        wait_failed(plane, session, "coder", run["run_id"])
        analyzed = plane.selfdev_analyze(session, "coder")
        with pytest.raises(Exception) as exc_info:
            plane.selfdev_apply(session, "coder",
                                analyzed["proposal"]["proposal_id"])
        assert "budget" in str(exc_info.value).lower()
        definition = plane.agent_definitions(session)["agents"][0]
        assert definition["generation"] == 4


def test_selfdev_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        client.post("/api/v1/agents", headers=headers,
                    json={"name": "spec-only", "role": "coding",
                          "capabilities": ["coding"]})
        run = client.post("/api/v1/agents/spec-only/run", headers=headers,
                          json={"requirement": "write something"})
        run_id = run.json()["run_id"]
        deadline = time.time() + 30.0
        state = {}
        while time.time() < deadline:
            state = client.get(
                f"/api/v1/agents/spec-only/runs/{run_id}",
                headers=headers).json()
            if state.get("status") in ("failed", "finished"):
                break
            time.sleep(0.05)
        assert state["status"] in ("failed", "finished")
        if state["status"] == "finished":
            assert state["run"]["success"] is False
        analyzed = client.post("/api/v1/agents/spec-only/selfdev/analyze",
                               headers=headers)
        assert analyzed.status_code == 200
        assert analyzed.json()["proposal"]["kind"] == "none"
        bad_apply = client.post(
            "/api/v1/agents/spec-only/selfdev/apply", headers=headers,
            json={"proposal_id": "does-not-exist"})
        assert bad_apply.status_code == 400
        ledger = client.get("/api/v1/agents/spec-only/selfdev",
                            headers=headers)
        assert ledger.status_code == 200
        assert len(ledger.json()["entries"]) == 1
