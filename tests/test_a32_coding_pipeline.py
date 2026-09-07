"""Model -> coder -> ChangeSet pipeline tests (A32.3 rebuild).

The model produces structured proposals (summary/changes/tests_to_run/
reasoning_summary/risk_level); the ChangeSet engine validates them and the
policy gate authorizes them before any file changes. Caller-supplied change
shortcuts are ignored: only model output drives writes.
"""
import json

import pytest

from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager


class _Scripted:
    name = "scripted"

    def __init__(self, response):
        self.response = response

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        return ModelResult(self.response, self.name)


def _fabric(payload):
    return ModelFabric(
        registry=ModelRegistry([Model(name="s/m", provider="s", capabilities=("coding",))]),
        providers=ProviderRegistry({"s": _Scripted(payload)}),
    )


def _request(approved=True, **metadata):
    task = TaskEngine().add("t", "add csv")
    return AgentRequest(task, TaskStatus.CODING, instructions="add csv",
                        metadata={"approved": approved, **metadata})


# -- schema ---------------------------------------------------------------

def test_parse_accepts_tests_to_run_and_risk_level():
    payload = json.dumps({
        "summary": "adds feature",
        "changes": [{"path": "app.py", "action": "modify",
                     "content": "x = 1\n", "risk": "low"}],
        "tests_to_run": ["tests/test_app.py"],
        "reasoning_summary": "small change",
        "risk_level": "medium",
        "risks": ["none"],
    })
    changes, extra = CoderAgent(".")._parse_changes(payload)
    assert changes == {"app.py": "x = 1\n"}
    assert extra["tests_to_run"] == ["tests/test_app.py"]
    assert extra["tests"] == ["tests/test_app.py"]  # alias preserved
    assert extra["risk_level"] == "MEDIUM"
    assert extra["change_meta"]["app.py"]["risk"] == "LOW"


def test_parse_defaults_risk_level_to_none():
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    _, extra = CoderAgent(".")._parse_changes(payload)
    assert extra["risk_level"] == "NONE"
    assert extra["tests_to_run"] == []
    assert extra["change_meta"]["app.py"]["risk"] == "NONE"


def test_parse_rejects_invalid_risk_level():
    payload = json.dumps({"changes": {"app.py": "x = 1\n"},
                          "risk_level": "extreme"})
    with pytest.raises(ValueError):
        CoderAgent(".")._parse_changes(payload)


def test_parse_rejects_invalid_per_change_risk():
    payload = json.dumps({"changes": [{"path": "app.py", "content": "x = 1\n",
                                       "risk": "bogus"}]})
    with pytest.raises(ValueError):
        CoderAgent(".")._parse_changes(payload)


def test_parse_captures_old_state_guards():
    payload = json.dumps({"changes": [{
        "path": "app.py", "content": "x = 2\n",
        "old_hash": "abc123", "old_content": "x = 1\n",
    }]})
    _, extra = CoderAgent(".")._parse_changes(payload)
    meta = extra["change_meta"]["app.py"]
    assert meta["expected_old_hash"] == "abc123"
    assert meta["expected_old_content"] == "x = 1\n"


def test_parse_rejects_non_string_old_guards():
    payload = json.dumps({"changes": [{
        "path": "app.py", "content": "x = 2\n", "old_hash": 42}]})
    with pytest.raises(ValueError):
        CoderAgent(".")._parse_changes(payload)


# -- pipeline -------------------------------------------------------------

def test_execute_surfaces_structured_metadata(tmp_path):
    payload = json.dumps({
        "summary": "export csv",
        "changes": [{"path": "app.py", "action": "modify",
                     "content": "x = 1\n"}],
        "tests_to_run": ["tests/test_csv.py"],
        "reasoning_summary": "added function",
        "risk_level": "low",
        "risks": [],
    })
    response = CoderAgent(root=str(tmp_path),
                          fabric=_fabric(payload)).execute(_request())
    assert response.success
    assert response.metadata["risk_level"] == "LOW"
    assert response.metadata["tests_to_run"] == ["tests/test_csv.py"]
    assert response.metadata["tests"] == ["tests/test_csv.py"]


def test_execute_enforces_model_old_content_guard(tmp_path):
    (tmp_path / "app.py").write_text("current\n")
    payload = json.dumps({"changes": [{
        "path": "app.py", "content": "x = 1\n", "old_content": "stale\n"}]})
    response = CoderAgent(root=str(tmp_path),
                          fabric=_fabric(payload)).execute(_request())
    assert not response.success
    assert (tmp_path / "app.py").read_text() == "current\n"


def test_execute_ignores_caller_supplied_changes_shortcut(tmp_path):
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    response = CoderAgent(
        root=str(tmp_path), fabric=_fabric(payload)).execute(_request(
            approved=True, changes={"evil.py": "pwned\n"}))
    assert response.success
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    assert not (tmp_path / "evil.py").exists()


def test_high_risk_change_blocked_in_autonomous_without_approval(tmp_path):
    payload = json.dumps({
        "changes": [{"path": "app.py", "content": "x = 1\n", "risk": "high"}],
        "risk_level": "high",
    })
    runtime = create_default_runtime(
        PermissionManager(mode=OperationMode.AUTONOMOUS), str(tmp_path))
    response = CoderAgent(runtime=runtime, root=str(tmp_path),
                          fabric=_fabric(payload)).execute(_request(approved=False))
    assert not response.success
    assert not (tmp_path / "app.py").exists()


def test_low_risk_change_auto_applies_in_autonomous(tmp_path):
    payload = json.dumps({
        "changes": [{"path": "app.py", "content": "x = 1\n", "risk": "low"}],
        "risk_level": "low",
    })
    runtime = create_default_runtime(
        PermissionManager(mode=OperationMode.AUTONOMOUS), str(tmp_path))
    response = CoderAgent(runtime=runtime, root=str(tmp_path),
                          fabric=_fabric(payload)).execute(_request(approved=False))
    assert response.success
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
