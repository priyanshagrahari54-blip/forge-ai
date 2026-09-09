"""A80 gate redesign tests (Phase 27).

GO requires A71-A79 rollout gates + a real SUCCEEDED run + security
invariants. The gate must additionally distinguish
ARCHITECTURE_COMPLETE (GO on simulators only) from PRODUCTION_READY
(GO with a real external provider configured) and emit machine-
readable reasons — never reporting production readiness while every
real provider is unconfigured.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.final.gate import external_provider_report  # noqa: E402
from helpers_a34 import (login, make_client, make_plane,  # noqa: E402
                         make_repo)


def gate_policy() -> Any:
    from forge.security.policy import PermissionPolicy, PermissionRule, Resource
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="REQUIRE_APPROVAL",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for key in ("OPENAI_API_KEY", "FORGE_SEARXNG_URL",
                "FORGE_COLAB_URL", "MODAL_TOKEN_ID",
                "FORGE_COMPUTE_SSH_HOST", "FORGE_COMPUTE_SSH_ALLOWLIST",
                "FORGE_DEPLOY_SSH_HOST", "FORGE_DEPLOY_SSH_ALLOWLIST",
                "FORGE_DEPLOY_SSH_PATH", "FORGE_VOICE_STT_PROVIDER",
                "FORGE_VOICE_TTS_PROVIDER", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def test_provider_report_is_deterministic():
    report = external_provider_report()
    for name, state in report.items():
        assert state["status"] in ("AVAILABLE", "UNAVAILABLE",
                                   "MISCONFIGURED")
    assert report["model-openai"]["status"] == "UNAVAILABLE"
    assert report["compute-ssh"]["status"] == "UNAVAILABLE"


def test_provider_report_detects_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    report = external_provider_report()
    assert report["model-openai"]["status"] == "AVAILABLE"
    assert report["vision-openai"]["status"] == "AVAILABLE"
    assert report["collaboration-openai"]["status"] == "AVAILABLE"
    assert report["research-web"]["status"] == "AVAILABLE"


def test_provider_report_flags_misconfigured_ssh(monkeypatch):
    monkeypatch.setenv("FORGE_COMPUTE_SSH_HOST", "deploy@host.example.org")
    report = external_provider_report()
    assert report["compute-ssh"]["status"] == "MISCONFIGURED"
    monkeypatch.setenv("FORGE_COMPUTE_SSH_ALLOWLIST", "host.example.org")
    report = external_provider_report()
    assert report["compute-ssh"]["status"] == "AVAILABLE"


def test_gate_without_real_providers_is_architecture_complete(tmp_path):
    from helpers_a34 import ScriptedProvider
    plane = make_plane(tmp_path, start=True, policy=gate_policy(),
                       provider=ScriptedProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_gate(session)
        assert report["decision"] == "GO"
        assert report["go"] is True
        assert report["capability_status"] == "ARCHITECTURE_COMPLETE"
        assert report["evidence"]["real_providers"] == []
        assert any("simulators" in reason for reason in report["reasons"])


def test_gate_with_real_provider_is_production_ready(tmp_path, monkeypatch):
    from helpers_a34 import ScriptedProvider
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    plane = make_plane(tmp_path, start=True, policy=gate_policy(),
                       provider=ScriptedProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_gate(session)
        assert report["decision"] == "GO"
        assert report["capability_status"] == "PRODUCTION_READY"
        assert "model-openai" in report["evidence"]["real_providers"]


def test_gate_no_go_has_machine_readable_reasons(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").chmod(0)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_gate(session)
        assert report["decision"] == "NO_GO"
        assert report["go"] is False
        assert report["capability_status"] == "NOT_READY"
        assert isinstance(report["reasons"], list)
        assert report["reasons"]
        assert any("rollout_passed" in reason
                   for reason in report["reasons"])
        assert report["evidence"]["succeeded_runs"] == 0
