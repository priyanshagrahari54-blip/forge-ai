"""A80 gate redesign tests — truthful provider-capability semantics.

GO requires A71-A79 rollout gates + a real SUCCEEDED run + security
invariants. Capability classification never trusts configuration
existence: an environment variable makes a provider CONFIGURED, and
only a fresh, explicit, successful capability verification makes it
VERIFIED. The gate itself never calls the network; verification
records are written by the explicit verification mechanism and read
back here.

- GO without any verified external provider -> ARCHITECTURE_COMPLETE.
- GO with a fresh VERIFIED external provider (and no unresolved
  configured-provider failure) -> PRODUCTION_READY.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.final.gate import external_provider_report  # noqa: E402
from forge.final.provider_verification import (  # noqa: E402
    DEFAULT_VERIFICATION_TTL, ProviderVerificationStore, run_verifications,
    verify_provider)
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


def _verified_record(name: str, *, age: float = 0.0,
                     status: str = "VERIFIED") -> dict[str, Any]:
    now = time.time()
    return {
        "provider": name, "status": status, "configured": True,
        "config_state": "CONFIGURED", "capability": "test capability",
        "checked_at": now, "verified_at": now - age,
        "evidence": {"provider": "test", "ok": True},
        "error": "", "note": "",
    }


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for key in ("OPENAI_API_KEY", "FORGE_SEARXNG_URL",
                "FORGE_COLAB_URL", "MODAL_TOKEN_ID",
                "FORGE_COMPUTE_SSH_HOST", "FORGE_COMPUTE_SSH_ALLOWLIST",
                "FORGE_DEPLOY_SSH_HOST", "FORGE_DEPLOY_SSH_ALLOWLIST",
                "FORGE_DEPLOY_SSH_PATH", "FORGE_VOICE_STT_PROVIDER",
                "FORGE_VOICE_TTS_PROVIDER", "OLLAMA_BASE_URL",
                "FORGE_PROVIDER_VERIFICATION_TTL"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# provider report: configuration != availability != verification
# ---------------------------------------------------------------------------

def test_provider_report_is_deterministic_and_offline():
    report = external_provider_report()
    for name, state in report.items():
        assert state["status"] in (
            "NOT_CONFIGURED", "CONFIGURED", "MISCONFIGURED", "VERIFIED",
            "DEGRADED", "AUTH_ERROR", "TIMEOUT", "RATE_LIMITED",
            "UNAVAILABLE", "PROVIDER_ERROR", "POLICY_DENIED")
    # Zero-key environment: every external provider is NOT_CONFIGURED.
    assert report["model-openai"]["status"] == "NOT_CONFIGURED"
    assert report["compute-ssh"]["status"] == "NOT_CONFIGURED"
    assert report["voice-whisper"]["status"] == "NOT_CONFIGURED"
    assert report["deploy-ssh-rsync"]["status"] == "NOT_CONFIGURED"
    # The local Ollama entry is CONFIGURED by implicit default but is a
    # local provider, never a proxy for external production readiness.
    assert report["ollama-local"]["kind"] == "local"
    assert report["ollama-local"]["status"] == "CONFIGURED"
    # No verification record exists: nothing is VERIFIED.
    assert all(state["status"] != "VERIFIED" for state in report.values())


def test_env_key_alone_never_means_verified(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    report = external_provider_report()
    # Configuration IS detected…
    assert report["model-openai"]["configured"] is True
    assert report["model-openai"]["config_state"] == "CONFIGURED"
    # …but the standing status is CONFIGURED, not AVAILABLE/VERIFIED.
    assert report["model-openai"]["status"] == "CONFIGURED"
    assert report["model-openai"]["verification"] is None
    assert "not capability-verified" in report["model-openai"]["note"].lower() \
        or "NOT capability-verified" in report["model-openai"]["note"]
    # Same for every openai-backed integration.
    for name in ("research-web", "vision-openai", "collaboration-openai",
                 "training-openai"):
        assert report[name]["status"] == "CONFIGURED"


def test_fresh_verified_record_is_verified(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    record = _verified_record("model-openai")
    report = external_provider_report(records={"model-openai": record})
    assert report["model-openai"]["status"] == "VERIFIED"
    assert report["model-openai"]["verification"]["fresh"] is True
    assert report["model-openai"]["verification"]["verified_at"] == \
        record["verified_at"]


def test_stale_verified_record_falls_back_to_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    stale = _verified_record(
        "model-openai", age=DEFAULT_VERIFICATION_TTL + 60)
    report = external_provider_report(records={"model-openai": stale})
    assert report["model-openai"]["status"] == "CONFIGURED"
    assert report["model-openai"]["verification"]["fresh"] is False
    assert "stale" in report["model-openai"]["note"].lower()


def test_fresh_failed_verification_is_an_explicit_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    failed = _verified_record("model-openai", status="AUTH_ERROR")
    report = external_provider_report(records={"model-openai": failed})
    assert report["model-openai"]["status"] == "AUTH_ERROR"
    assert report["model-openai"]["verification"]["fresh"] is True


def test_unconfigured_with_old_record_is_still_not_configured():
    record = _verified_record("model-openai")
    report = external_provider_report(records={"model-openai": record})
    # Configuration vanished (env cleaned): status NOT_CONFIGURED.
    assert report["model-openai"]["configured"] is False
    assert report["model-openai"]["status"] == "NOT_CONFIGURED"


def test_provider_report_flags_misconfigured_ssh(monkeypatch):
    monkeypatch.setenv("FORGE_COMPUTE_SSH_HOST", "worker.internal")
    report = external_provider_report()
    assert report["compute-ssh"]["status"] == "MISCONFIGURED"
    assert report["compute-ssh"]["configured"] is True
    assert "allowlist" in report["compute-ssh"]["note"].lower()


def test_provider_report_flags_misconfigured_voice(monkeypatch):
    monkeypatch.setenv("FORGE_VOICE_STT_PROVIDER", "openai-whisper")
    report = external_provider_report()
    # Whisper selected but OPENAI_API_KEY missing -> MISCONFIGURED.
    assert report["voice-whisper"]["status"] == "MISCONFIGURED"


# ---------------------------------------------------------------------------
# verification runner (probes stubbed — never touches the network)
# ---------------------------------------------------------------------------

def test_verify_unconfigured_provider_records_not_configured_no_network(
        tmp_path, monkeypatch):
    from forge.control import ControlConfig, ControlPlane
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "v.db"), projects={"demo": str(tmp_path)},
        fabric=None))
    store = plane.provider_verifications
    probe_calls: list[str] = []

    import forge.final.provider_verification as pv
    monkeypatch.setattr(pv, "_probe",
                        lambda name: probe_calls.append(name) or
                        {"state": "VERIFIED", "evidence": {}})
    results = run_verifications(store)
    # No external provider is configured: only the implicit local
    # ollama endpoint may be probed — never any external network.
    assert "model-openai" not in probe_calls
    assert results["model-openai"]["status"] == "NOT_CONFIGURED"
    assert store.get("model-openai")["status"] == "NOT_CONFIGURED"
    assert results["compute-ssh"]["status"] == "NOT_CONFIGURED"
    assert store.get("compute-ssh")["status"] == "NOT_CONFIGURED"
    plane.close()


def test_verify_openai_success_writes_verified_record(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    from forge.control import ControlConfig, ControlPlane
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "v.db"), projects={"demo": str(tmp_path)}))
    try:
        store = plane.provider_verifications
        calls: list[str] = []

        import forge.final.provider_verification as pv

        def fake_post_json(url, body, headers, timeout):
            calls.append(url)
            assert "Bearer sk-real-key-0123456789abcdef" in \
                headers["Authorization"]
            return "SUCCESS", {"id": "x", "choices": [{"index": 0}]}

        monkeypatch.setattr(pv, "_post_json", fake_post_json)
        results = run_verifications(store)
        assert calls, "openai endpoint was not probed"
        assert results["model-openai"]["status"] == "VERIFIED"
        assert results["model-openai"]["verified_at"] is not None
        # Every openai-backed provider shares the verified outcome.
        for name in ("collaboration-openai", "research-web"):
            assert results[name]["status"] == "VERIFIED"
        stored = store.all()
        assert stored["model-openai"]["status"] == "VERIFIED"
        # Redaction: the key never lands in stored evidence/error.
        blob = repr(stored)
        assert "sk-real-key-0123456789abcdef" not in blob
    finally:
        plane.close()


def test_verify_openai_auth_error_is_recorded_not_success(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    from forge.control import ControlConfig, ControlPlane
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "v.db"), projects={"demo": str(tmp_path)}))
    try:
        store = plane.provider_verifications
        import forge.final.provider_verification as pv
        monkeypatch.setattr(
            pv, "_post_json",
            lambda *a, **k: ("AUTH_ERROR", None))
        results = run_verifications(store)
        assert results["model-openai"]["status"] == "AUTH_ERROR"
        assert results["model-openai"]["verified_at"] is None
        assert store.get("model-openai")["status"] == "AUTH_ERROR"
    finally:
        plane.close()


def test_verify_returns_error_for_unknown_provider(tmp_path):
    from forge.control import ControlConfig, ControlPlane
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "v.db"), projects={"demo": str(tmp_path)}))
    try:
        with pytest.raises(ValueError):
            verify_provider("not-a-provider")
    finally:
        plane.close()


# ---------------------------------------------------------------------------
# final gate classification
# ---------------------------------------------------------------------------

def _session(plane, client):
    from helpers_a34 import login
    payload, _token, _headers = login(client, project_id="demo")
    return plane.sessions.get(payload["session_id"])


def test_gate_without_any_real_provider_is_architecture_complete(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        # Gate's own acceptance smoke produces the SUCCEEDED run.
        assert report["go"] is True
        assert report["capability_status"] == "ARCHITECTURE_COMPLETE"
        assert report["production_readiness"]["real_provider_configured"] \
            is False
        assert report["evidence"]["configured_providers"] == []
        assert report["evidence"]["verified_providers"] == []
        assert any("ARCHITECTURE_COMPLETE" in reason
                   for reason in report["reasons"])


def test_gate_configured_but_unverified_provider_is_still_architecture(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is True
        # A key alone must NOT flip the gate to PRODUCTION_READY.
        assert report["capability_status"] == "ARCHITECTURE_COMPLETE"
        assert report["evidence"]["configured_providers"] == sorted([
            "model-openai", "research-web", "vision-openai",
            "collaboration-openai", "training-openai"])
        assert report["evidence"]["verified_providers"] == []
        assert any("capability-VERIFIED" in reason
                   for reason in report["reasons"])


def test_gate_with_verified_provider_is_production_ready(tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    record = _verified_record("model-openai")
    plane.provider_verifications.save("model-openai", record)
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is True
        assert report["capability_status"] == "PRODUCTION_READY"
        assert report["evidence"]["verified_providers"] == ["model-openai"]
        assert report["production_readiness"]["provider_verified"] is True
        assert report["production_readiness"]["evidence_recent"] is True
        assert report["production_readiness"] \
            ["no_unresolved_provider_failure"] is True


def test_gate_stale_verification_is_not_production_ready(tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    monkeypatch.setenv("FORGE_PROVIDER_VERIFICATION_TTL", "30")
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    stale = _verified_record("model-openai", age=3600.0)
    plane.provider_verifications.save("model-openai", stale)
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is True
        assert report["capability_status"] == "ARCHITECTURE_COMPLETE"
        assert report["evidence"]["verified_providers"] == []
        assert any("stale" in reason.lower() for reason in report["reasons"]) \
            or report["evidence"]["provider_report"]["model-openai"] \
            ["verification"]["fresh"] is False


def test_gate_unresolved_provider_failure_blocks_production_ready(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    monkeypatch.setenv("FORGE_SEARXNG_URL",
                       "https://searxng.example.invalid/search")
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    # model-openai verified, research-web freshly failed (auth).
    plane.provider_verifications.save(
        "model-openai", _verified_record("model-openai"))
    plane.provider_verifications.save(
        "research-web",
        _verified_record("research-web", status="AUTH_ERROR"))
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is True
        # A verified provider exists, but an unresolved configured
        # failure remains -> NOT production ready.
        assert report["capability_status"] == "ARCHITECTURE_COMPLETE"
        assert report["evidence"]["verified_providers"] == ["model-openai"]
        assert report["evidence"]["unresolved_provider_failures"] == \
            ["research-web"]
        assert report["production_readiness"] \
            ["no_unresolved_provider_failure"] is False
        assert any("unresolved" in reason.lower()
                   for reason in report["reasons"])


def test_gate_no_go_has_machine_readable_reasons(tmp_path):
    # No successful run on record -> NO_GO with machine-readable
    # requirements and reasons.
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    root = Path(plane.projects["demo"].root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def health(): return True\n")
    client = make_client(plane)
    with client:
        session = _session(plane, client)
        report = plane.final_gate(session)
        assert report["go"] is False
        assert report["decision"] == "NO_GO"
        assert report["capability_status"] == "NOT_READY"
        assert report["requirements"]["rollout_passed"] is False
        assert report["requirements"]["real_successful_run"] is False
        assert report["reasons"], "NO_GO must carry machine-readable reasons"
        assert report["evidence"]["verification_policy"]["ttl_seconds"] > 0


def test_gate_verify_api_route_is_explicit_and_bounded(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-0123456789abcdef")
    plane = make_plane(tmp_path, start=True, policy=gate_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)

    import forge.final.provider_verification as pv

    def fake_post_json(url, body, headers, timeout):
        return "SUCCESS", {"id": "x", "choices": [{"index": 0}]}

    monkeypatch.setattr(pv, "_post_json", fake_post_json)
    with client:
        session, token, headers = login(client, project_id="demo")
        response = client.post("/api/v1/final/gate/verify", headers=headers,
                               json={"provider": "model-openai"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["go"] is True
        assert body["capability_status"] == "PRODUCTION_READY"
        assert "model-openai" in body["evidence"]["verified_providers"]
        # Unknown provider -> honest 400.
        bad = client.post("/api/v1/final/gate/verify", headers=headers,
                          json={"provider": "nope"})
        assert bad.status_code == 400
        assert session["session_id"]
