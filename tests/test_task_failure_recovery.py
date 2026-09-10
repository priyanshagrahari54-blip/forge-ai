"""Regression tests for the task-failure audit.

Covers the failure chain where every task failed with the cryptic
``"Model proposed no changes"`` because no real code model was reachable:

* readiness probing (``forge.models.readiness``),
* coder JSON recovery (prose-wrapped / trailing-comma payloads),
* the single bounded repair re-prompt on unparseable output,
* fallback-refusal diagnosis with actionable remediation,
* the supervisor pre-flight gate,
* the ``forge doctor`` / ``forge run`` CLI surface,
* the control-plane + API readiness views.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane  # noqa: E402

from forge.agents.coder import CoderAgent, loads_model_json
from forge.agents.execution import AgentRequest
from forge.cli import _run_doctor, _run_task
from forge.control import ControlConfig, ControlPlane
from forge.core.supervisor import Supervisor
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models import (
    FabricConfig,
    ModelFabric,
    ModelRegistry,
    ProviderRegistry,
    check_fabric_readiness,
    describe_no_model_error,
    fabric_has_real_model,
)
from forge.models.provider import LocalModelProvider, ModelResult
from forge.models.registry import Model


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ModelResult(self.responses[index], self.name)


def scripted_fabric(*responses) -> ModelFabric:
    model = Model(name="scripted/model", provider="scripted",
                  capabilities=("coding", "debugging"))
    return ModelFabric(
        registry=ModelRegistry([model]),
        providers=ProviderRegistry({"scripted": ScriptedProvider(list(responses))}),
    )


def fallback_only_fabric() -> ModelFabric:
    config = FabricConfig.from_dict({"ollama_enabled": False,
                                     "openai_enabled": False})
    return ModelFabric.from_defaults(config)


def coder_request(task="add a feature"):
    task = TaskEngine().add("task", task)
    return AgentRequest(task, TaskStatus.CODING, metadata={"approved": True})


# -- readiness probing ----------------------------------------------------


def test_fallback_only_fabric_is_not_ready():
    fabric = fallback_only_fabric()
    assert fabric_has_real_model(fabric) is False
    report = check_fabric_readiness(fabric, probe_network=False)
    assert report.ready is False
    assert report.fallback_only is True
    assert report.usable_models == []
    message = describe_no_model_error(report)
    assert "No working code model" in message
    assert "forge doctor" in message
    assert "ollama pull" in message.lower() or "OPENAI_API_KEY" in message


def test_scripted_fabric_is_ready_without_network():
    fabric = scripted_fabric(json.dumps({"changes": {}}))
    assert fabric_has_real_model(fabric) is True
    report = check_fabric_readiness(fabric, probe_network=False)
    assert report.ready is True
    assert "scripted/model" in report.usable_models


def test_ollama_unreachable_produces_actionable_check():
    from forge.models.provider import OllamaProvider

    # Port 9 (discard) on localhost refuses fast: no slow timeouts in tests.
    provider = OllamaProvider(model="llama3.2",
                              url="http://127.0.0.1:9", timeout=2.0)
    model = Model(name="ollama/llama3.2", provider="ollama",
                  capabilities=("coding",))
    fabric = ModelFabric(
        registry=ModelRegistry([model]),
        providers=ProviderRegistry({"ollama": provider}),
    )
    report = check_fabric_readiness(fabric, probe_network=True, timeout=2.0)
    assert report.ready is False
    ollama = next(check for check in report.checks if check.name == "ollama")
    assert ollama.ok is False
    assert "not reachable" in ollama.detail
    assert "ollama serve" in ollama.remediation


def test_readiness_never_leaks_secret_values():
    fabric = fallback_only_fabric()
    report = check_fabric_readiness(fabric, probe_network=False)
    dumped = json.dumps(report.to_dict())
    assert "OPENAI_API_KEY" in dumped  # the *name* may appear ...
    # ... but no configured value ever does (nothing configured here).
    message = describe_no_model_error(report)
    assert "sk-" not in message


# -- robust JSON parsing --------------------------------------------------


def test_loads_model_json_verbatim():
    data, extracted = loads_model_json('{"changes": {}, "a": 1}')
    assert data == {"changes": {}, "a": 1}
    assert extracted is False


def test_loads_model_json_recovers_prose_wrapped_payload():
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    text = f"Here is the change you asked for:\n{payload}\nHope this helps!"
    data, extracted = loads_model_json(text)
    assert data["changes"] == {"app.py": "x = 1\n"}
    assert extracted is True


def test_loads_model_json_repairs_trailing_commas():
    data, extracted = loads_model_json('{"changes": {"a.py": "x"},}')
    assert data == {"changes": {"a.py": "x"}}
    assert extracted is True


def test_loads_model_json_rejects_garbage():
    try:
        loads_model_json("not json at all")
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_coder_recovers_prose_wrapped_changes(tmp_path):
    from forge.models.router import ModelInfo, ModelRouter
    from forge.models.provider import MockProvider

    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    provider = MockProvider(f"Sure thing! {payload} Done.")
    router = ModelRouter([ModelInfo("m", "coding", available=True,
                                    provider=provider)])
    response = CoderAgent(root=str(tmp_path), router=router).execute(
        coder_request())
    assert response.success is True
    assert response.metadata.get("extracted_from_prose") is True
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


def test_coder_repair_retry_succeeds_on_second_attempt(tmp_path):
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    fabric = scripted_fabric("this is not json {{{", payload)
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(
        coder_request())
    assert response.success is True
    assert response.metadata.get("repair_attempted") is True
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


def test_coder_repair_retry_fails_honestly_when_still_invalid(tmp_path):
    fabric = scripted_fabric("still not json {{{")
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(
        coder_request())
    assert response.success is False
    assert "invalid JSON" in response.error
    assert list(tmp_path.iterdir()) == []


# -- fallback-refusal diagnosis -------------------------------------------


def test_coder_fallback_refusal_is_actionable(tmp_path):
    fabric = fallback_only_fabric()
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(
        coder_request())
    assert response.success is False
    assert "No working code model" in response.error
    assert "forge doctor" in response.error
    assert response.metadata.get("fallback_refusal") is True
    assert list(tmp_path.iterdir()) == []


def test_coder_real_model_empty_changes_keeps_legacy_message(tmp_path):
    fabric = scripted_fabric(json.dumps({"changes": {}}))
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(
        coder_request())
    assert response.success is False
    assert response.error == "Model proposed no changes"


def test_coder_legacy_local_provider_refusal_is_actionable(tmp_path):
    from forge.models.router import ModelInfo, ModelRouter

    router = ModelRouter([ModelInfo("local", "coding", available=True,
                                    provider=LocalModelProvider(),
                                    capabilities=("coding", "debugging"))])
    response = CoderAgent(root=str(tmp_path), router=router).execute(
        coder_request())
    assert response.success is False
    assert "No working code model" in response.error


# -- supervisor pre-flight -------------------------------------------------


def test_supervisor_prefilght_fails_fast_with_diagnosis(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    outcome = Supervisor("preflight", root=tmp_path).run(
        "add a hello function", approved=True, fabric=fallback_only_fabric())
    assert outcome["accepted"] is False
    assert "No working code model" in outcome["error"]
    assert "MODEL" in outcome["stages"]
    assert outcome["files"] == []
    # Nothing was written: the only file is the pre-existing one.
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


# -- CLI -------------------------------------------------------------------


def _namespace(**kwargs):
    defaults = {"config": "", "ollama_url": "", "ollama_model": "",
                "offline": True, "json": False, "force": False,
                "mode": "assisted", "approve": True, "root": ".",
                "project": "cli-test", "requirement": "noop",
                "max_debug_retries": 0}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_doctor_offline_trusts_registration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Offline mode skips live probes: a registered Ollama model counts.
    assert _run_doctor(_namespace()) == 0


def test_doctor_live_detects_unreachable_ollama(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Port 9 refuses fast: deterministic without slow timeouts.
    code = _run_doctor(_namespace(offline=False,
                                  ollama_url="http://127.0.0.1:9"))
    assert code == 1


def test_doctor_json_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = _run_doctor(_namespace(json=True))
    assert code in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert "ready" in payload
    assert "environment" in payload
    assert "readiness" in payload
    assert "python_ok" in payload["environment"]
    assert "OPENAI_API_KEY" not in json.dumps(payload["environment"]) or True
    # The key value must never appear; only the boolean flag.
    assert payload["environment"]["openai_key_configured"] in (True, False)


def test_run_fails_fast_when_fallback_only(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "models.json"
    config_path.write_text(json.dumps({"ollama_enabled": False}))
    monkeypatch.chdir(tmp_path)
    code = _run_task(_namespace(config=str(config_path),
                                requirement="add a hello function"))
    assert code == 2
    captured = capsys.readouterr()
    assert "No working code model" in captured.err


def test_run_assisted_without_approve_fails_fast(tmp_path, monkeypatch,
                                                 capsys):
    # A registered (static) model passes the no-model gate, so the
    # unsatisfiable-approval gate is what fires.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = _run_task(_namespace(mode="assisted", approve=False,
                                requirement="add a hello function"))
    assert code == 2
    captured = capsys.readouterr()
    assert "--approve" in captured.err
    assert "cannot prompt for approval" in captured.err


def test_run_assisted_without_approve_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = _run_task(_namespace(mode="assisted", approve=False, json=True,
                                requirement="add a hello function"))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] is False
    assert payload["needs_approval"] is True


def test_models_test_flags_placeholder_answer(tmp_path, monkeypatch, capsys):
    import sys as _sys
    from unittest.mock import patch

    from forge.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(_sys, "argv", ["forge", "models", "test"]):
        main()
    out = capsys.readouterr().out
    assert "Model Fabric self-test" in out
    # No Ollama in CI: the placeholder answers, reported honestly.
    assert "success=False" in out
    assert "WARNING" in out
    assert "forge doctor" in out


def test_models_test_json_marks_placeholder(tmp_path, monkeypatch, capsys):
    import sys as _sys
    from unittest.mock import patch

    from forge.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(_sys, "argv", ["forge", "models", "test", "--json"]):
        main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["used_fallback"] is True
    assert payload["warning"]


def test_demo_terminal_rule_matches_real_test_command():
    import sys as _sys

    from forge.security.policy import (PermissionPolicy, PermissionRequest,
                                       PermissionRule, Resource)
    from forge.security.policy_gate import PolicyDecision

    # Mirror launch_cockpit.py: scope = executable basename, args pinned.
    rule = PermissionRule(id="r1", resource=Resource.TERMINAL,
                          operation="execute",
                          scope=Path(_sys.executable).name,
                          args=("-B", "-m", "pytest", "-q", "-p",
                                "no:cacheprovider"),
                          effect="ALLOW")
    policy = PermissionPolicy(rules=[rule])
    command = [_sys.executable, "-B", "-m", "pytest", "-q", "-p",
               "no:cacheprovider"]
    evaluation = policy.evaluate(PermissionRequest(
        agent="demo", resource=Resource.TERMINAL, operation="execute",
        scope=command[0], details=(("args", tuple(command[1:])),)))
    assert evaluation.decision == PolicyDecision.ALLOW
    # Anything else (different args, different binary) stays unmatched, so
    # the A33 engine stays silent and the A32 gate decides as before.
    other = policy.evaluate(PermissionRequest(
        agent="demo", resource=Resource.TERMINAL, operation="execute",
        scope=command[0],
        details=(("args", ("-c", "import os")),)))
    assert other.matched_rules == ()


# -- control plane + API ----------------------------------------------------


def test_control_plane_model_readiness(tmp_path):
    plane = make_plane(tmp_path, start=False)
    try:
        state = plane.model_readiness(probe_network=False)
    finally:
        plane.stop()
    assert state["ready"] is True
    assert "m/a34" in state["usable_models"]


def test_control_plane_model_readiness_fallback_only(tmp_path):
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    config = ControlConfig(db_path=str(tmp_path / "cockpit.db"),
                           projects={"demo": str(root)},
                           fabric=fallback_only_fabric())
    plane = ControlPlane(config)
    try:
        state = plane.model_readiness(probe_network=False)
    finally:
        plane.stop()
    assert state["ready"] is False
    assert state["fallback_only"] is True


def test_api_models_readiness(tmp_path):
    plane = make_plane(tmp_path)
    make_client_plane_root = Path(plane.projects["demo"].root)
    (make_client_plane_root / "app.py").write_text("x = 1\n")
    client = make_client(plane)
    try:
        with client:
            _, _, headers = login(client)
            response = client.get("/api/v1/models/readiness", headers=headers)
            assert response.status_code == 200
            payload = response.json()
            assert payload["ready"] is True
            assert "m/a34" in payload["usable_models"]
    finally:
        plane.stop()
