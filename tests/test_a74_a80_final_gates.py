"""Final gates A74-A80: benchmark, commit, memory, self-evaluation,
rollout, loop, and go/no-go."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.models.provider import ModelResult  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import (ScriptedProvider, login, make_client,  # noqa: E402
                         make_plane, make_repo)


class BenchProvider(ScriptedProvider):
    """Deterministic provider answering the three benchmark checks."""

    name = "bench"

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if "benchmark" in prompt and "value" in prompt:
            return ModelResult('{"benchmark": true, "value": 42}',
                               self.name)
        if "17 + 25" in prompt:
            return ModelResult("42", self.name)
        return ModelResult("FORGE-BENCHMARK-OK", self.name)


def gate_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="deny-net", resource=Resource.NETWORK,
                       operation="request", scope="example.com",
                       effect="DENY"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="REQUIRE_APPROVAL",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


def _session(plane, client):
    payload, _token, headers = login(client)
    return plane.sessions.get(payload["session_id"]), headers


def test_benchmark_gate_code_judged(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy(),
                       provider=BenchProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_benchmark_gate(session, min_passed=1)
        assert report["passed"] is True
        assert report["summary"]["passed"] == 3
        assert report["summary"]["models_benchmarked"] >= 1
        assert report["all_models_benchmarked"] is True
        assert "grade themselves" in report["note"]
        impossible = plane.final_benchmark_gate(session, min_passed=8)
        assert impossible["passed"] is False
        response = client.post("/api/v1/final/benchmark",
                               headers=headers, json={"min_passed": 9})
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_REQUEST"


def test_commit_gate_reports_readiness(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_commit_gate(session)
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["repository"]["passed"] is True
        assert checks["head_exists"]["passed"] is True
        assert report["passed"] is True
        response = client.post("/api/v1/final/commit", headers=headers)
        assert response.status_code == 200


def test_memory_gate_checks_ledger_and_table(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_memory_gate(session)
        assert report["passed"] is True
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["learning_ledger"]["passed"] is True
        assert checks["session_memory_table"]["passed"] is True
        response = client.post("/api/v1/final/memory", headers=headers)
        assert response.status_code == 200
        assert response.json()["passed"] is True


def test_self_evaluation_is_unproven_without_evidence(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_self_evaluation(session)
        assert report["grade"] == "unproven"
        assert report["findings"]["tasks_submitted"] == 0
        assert "never a claimed capability" in report["note"]
        response = client.post("/api/v1/final/self-evaluation",
                               headers=headers)
        assert response.status_code == 200


def test_rollout_gate_on_clean_plane_and_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_rollout(session)
        assert report["passed"] is True
        for gate in ("acceptance", "security", "benchmark", "commit",
                     "memory"):
            assert report["gates"][gate] is True
        assert report["gates"]["smoke_verification"] is True
        verification = report["details"]["smoke_verification"]
        assert verification["verified"] is True
        response = client.post("/api/v1/final/rollout", headers=headers)
        assert response.status_code == 200


def test_final_loop_is_bounded_and_real(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        report = plane.final_loop(session, max_iterations=3)
        assert report["passed"] is True
        assert 1 <= report["iterations"] <= 3
        assert report["max_iterations"] == 3
        assert len(report["history"]) == report["iterations"]
        assert all(iteration["gates"] for iteration in
                   report["history"][:-1])
        response = client.post("/api/v1/final/loop", headers=headers,
                               json={"max_iterations": 99})
        assert response.status_code == 400


def test_final_gate_requires_a_real_successful_run(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        # The gate's own acceptance smoke produces a genuinely
        # SUCCEEDED run, so the end-to-end demonstration exists.
        report = plane.final_gate(session)
        assert report["go"] is True
        assert report["requirements"]["rollout_passed"] is True
        assert report["requirements"]["real_successful_run"] is True
        assert report["evidence"]["succeeded_runs"] >= 1
        response = client.post("/api/v1/final/gate", headers=headers)
        assert response.status_code == 200
        assert response.json()["go"] is True


def test_final_gate_refuses_without_any_successful_run(tmp_path):
    # Broken project: the smoke run genuinely fails, so there is no
    # SUCCEEDED run on record and the gate refuses honestly.
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").chmod(0)
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is False
        assert report["requirements"]["rollout_passed"] is False
        assert report["requirements"]["real_successful_run"] is False
        assert report["evidence"]["succeeded_runs"] == 0


def test_degraded_plane_fails_rollout_honestly(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "leak.txt").write_text(
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD\n",
        encoding="utf-8")
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        report = plane.final_rollout(session)
        assert report["passed"] is False
        assert report["gates"]["security"] is False
        assert report["gates"]["acceptance"] is True
        gate = plane.final_gate(session)
        assert gate["go"] is False
