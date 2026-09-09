"""Model benchmarking (A60): honest check-based harness."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.models.provider import ModelResult  # noqa: E402
from helpers_a34 import (ScriptedProvider, login, make_client,  # noqa: E402
                         make_plane, make_repo)


class BenchProvider(ScriptedProvider):
    """Deterministic provider that answers the three benchmark checks."""

    name = "bench"

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if "benchmark" in prompt and "value" in prompt:
            return ModelResult('{"benchmark": true, "value": 42}',
                               self.name)
        if "17 + 25" in prompt:
            return ModelResult("42", self.name)
        return ModelResult("FORGE-BENCHMARK-OK", self.name)


def test_benchmark_judges_real_responses(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=BenchProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.benchmark_run(session)
        assert report["summary"]["models_benchmarked"] == 1
        assert report["summary"]["passed"] == 3
        assert report["summary"]["total"] == 3
        assert report["summary"]["honest"] is True
        entry = report["benchmarks"][0]
        assert entry["model"] == "m/a34"
        assert entry["provider"] == "p"
        assert entry["passed"] == 3
        check_names = {check["check"] for check in entry["checks"]}
        assert check_names == {"json-object", "arithmetic",
                               "marker-echo"}
        # Prompts really went through the provider.
        assert len(plane.fabric.providers.get("p").prompts) == 3


def test_benchmark_reports_honest_failures(tmp_path):
    plane = make_plane(tmp_path, start=True)  # default ScriptedProvider
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.benchmark_run(session)
        # The scripted CSV payload satisfies none of the checks; the
        # harness says so instead of faking capability.
        assert report["summary"]["passed"] == 0
        assert report["summary"]["total"] == 3
        entry = report["benchmarks"][0]
        assert entry["passed"] == 0
        assert all(check["passed"] is False
                   for check in entry["checks"])


def test_benchmark_validation_and_history(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=BenchProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.benchmark_run(session, models=["ghost-model"])
        with pytest.raises(Exception):
            plane.benchmark_run(session, models=["m/a34"] * 6)
        with pytest.raises(Exception):
            plane.benchmark_history(session, limit=101)
        plane.benchmark_run(session)
        history = plane.benchmark_history(session)
        assert len(history["benchmarks"]) == 1
        assert plane.benchmark_history(session)["benchmarks"][0][
            "passed"] == 3


def test_benchmark_history_persists_across_restarts(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=BenchProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.benchmark_run(session)
        plane.stop(wait=True)
        plane2 = make_plane(tmp_path, start=True)
        client2 = make_client(plane2)
        with client2:
            payload2, _token2, _headers2 = login(client2)
            session2 = plane2.sessions.get(payload2["session_id"])
            history = plane2.benchmark_history(session2)
            assert len(history["benchmarks"]) == 1
            assert history["benchmarks"][0]["passed"] == 3


def test_benchmark_api(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=BenchProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        run = client.post("/api/v1/benchmarks", headers=headers,
                          json={"models": ["m/a34"]})
        assert run.status_code == 200
        body = run.json()
        assert body["summary"]["passed"] == 3
        assert len(body["benchmarks"]) == 1
        history = client.get("/api/v1/benchmarks", headers=headers)
        assert history.status_code == 200
        assert len(history.json()["benchmarks"]) == 1
        bad = client.post("/api/v1/benchmarks", headers=headers,
                          json={"models": ["ghost-model"]})
        assert bad.status_code == 400
