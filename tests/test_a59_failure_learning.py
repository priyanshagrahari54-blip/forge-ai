"""Failure learning (A59): persistent bounded failure ledger."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.models.provider import ModelResult  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import (ScriptedProvider, drive_to_terminal,  # noqa: E402
                         login, make_client, make_plane, make_repo)


def run_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


class BadJsonProvider(ScriptedProvider):
    """Deterministic provider that answers with invalid JSON."""

    name = "bad-json"

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ModelResult("not valid json", self.name)


def test_task_failures_are_learned_automatically(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=BadJsonProvider(),
                       policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "write a helper module")
        state = drive_to_terminal(client, headers, task.id)
        assert state["status"] == "FAILED"
        lessons = plane.failure_lessons(session)["lessons"]
        task_lessons = [entry for entry in lessons
                        if entry["category"] == "task"]
        assert task_lessons
        assert any("invalid json" in entry["fingerprint"]
                   for entry in task_lessons)


def test_agent_failures_are_learned_automatically(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "spec-only", "coding", ["coding"])
        run = plane.agent_run(session, "spec-only", "write something")
        deadline = time.time() + 30.0
        while time.time() < deadline:
            state = plane.agent_run_result(session, "spec-only",
                                           run["run_id"])
            if state["status"] in ("failed", "finished"):
                break
            time.sleep(0.05)
        lessons = plane.failure_lessons(session)["lessons"]
        agent_lessons = [entry for entry in lessons
                         if entry["category"] == "agent"]
        assert any("without a bound executor" in entry["lesson"]
                   for entry in agent_lessons)


def test_deduplication_counts_and_validation(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        for _ in range(3):
            plane.record_failure(session, "system",
                                 "database lock contention")
        for _ in range(2):
            plane.record_failure(session, "model", "provider timeout")
        top = plane.failure_lessons(session)["top"]
        by_fingerprint = {entry["fingerprint"]: entry for entry in top}
        assert by_fingerprint["database lock contention"]["count"] == 3
        assert by_fingerprint["provider timeout"]["count"] == 2
        stats = plane.failure_lessons(session)["stats"]
        assert stats["total_events"] == 5
        assert stats["distinct_keys"] == 2
        with pytest.raises(Exception):
            plane.record_failure(session, "bogus", "something failed")
        with pytest.raises(Exception):
            plane.record_failure(session, "system", "")


def test_learning_persists_across_plane_restarts(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.record_failure(session, "system", "restart resilience check")
        plane.stop(wait=True)
        plane2 = make_plane(tmp_path, start=True, policy=run_policy())
        client2 = make_client(plane2)
        with client2:
            payload2, _token2, _headers2 = login(client2)
            session2 = plane2.sessions.get(payload2["session_id"])
            lessons = plane2.failure_lessons(session2)["lessons"]
            assert any(
                "restart resilience" in entry["fingerprint"]
                for entry in lessons)


def test_learning_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        recorded = client.post("/api/v1/learning/failures", headers=headers,
                               json={"category": "system",
                                     "error": "api level failure"})
        assert recorded.status_code == 200
        assert recorded.json()["count"] == 1
        bad = client.post("/api/v1/learning/failures", headers=headers,
                          json={"category": "nope", "error": "x"})
        assert bad.status_code == 400
        lessons = client.get("/api/v1/learning/lessons", headers=headers)
        assert lessons.status_code == 200
        body = lessons.json()
        assert any("api level failure" in entry["fingerprint"]
                   for entry in body["lessons"])
        assert body["stats"]["total_events"] >= 1
