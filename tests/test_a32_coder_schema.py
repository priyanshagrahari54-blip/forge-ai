"""Coder structured-change schema tests (A32.3)."""
import json

import pytest

from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.router import ModelInfo, ModelRouter


def _coder(root, fabric=None, router=None):
    return CoderAgent(root=str(root), fabric=fabric, router=router)


def test_parse_changes_accepts_list_schema():
    payload = json.dumps({
        "summary": "adds feature",
        "changes": [
            {"path": "app.py", "action": "modify", "content": "def ok(): return 1\n"},
            {"path": "new_mod.py", "action": "create", "content": "def f(): return 2\n"},
        ],
        "tests": ["tests/test_app.py"],
        "reasoning_summary": "small change",
        "risks": ["none"],
    })
    changes, extra = _coder(".")._parse_changes(payload)
    assert changes == {"app.py": "def ok(): return 1\n", "new_mod.py": "def f(): return 2\n"}
    assert extra["summary"] == "adds feature"
    assert extra["tests"] == ["tests/test_app.py"]
    assert extra["reasoning_summary"] == "small change"
    assert extra["risks"] == ["none"]


def test_parse_changes_accepts_legacy_map_schema():
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}, "explanation": "ok"})
    changes, _extra = _coder(".")._parse_changes(payload)
    assert changes == {"app.py": "x = 1\n"}


def test_parse_changes_rejects_delete_action():
    payload = json.dumps({"changes": [{"path": "app.py", "action": "delete", "content": ""}]})
    with pytest.raises(ValueError):
        _coder(".")._parse_changes(payload)


def test_parse_changes_rejects_missing_changes():
    with pytest.raises(ValueError):
        _coder(".")._parse_changes(json.dumps({"summary": "nothing"}))


def test_coder_execute_writes_list_schema_and_surfaces_metadata(tmp_path):
    payload = json.dumps({
        "summary": "export csv",
        "changes": [{"path": "app.py", "action": "modify", "content": "def export_csv(): return 'name,score'\n"}],
        "tests": ["tests/test_csv.py"],
        "reasoning_summary": "added function",
        "risks": [],
    })
    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="s/m", provider="s", capabilities=("coding",))]),
        providers=ProviderRegistry({"s": _Scripted(payload)}),
    )
    task = TaskEngine().add("t", "add csv")
    request = AgentRequest(task, TaskStatus.CODING, instructions="add csv", metadata={"approved": True})
    response = _coder(tmp_path, fabric=fabric).execute(request)

    assert response.success
    assert (tmp_path / "app.py").read_text() == "def export_csv(): return 'name,score'\n"
    assert response.metadata["summary"] == "export csv"
    assert response.metadata["tests"] == ["tests/test_csv.py"]
    assert response.metadata["reasoning_summary"] == "added function"


def test_coder_execute_legacy_router_path_still_works(tmp_path):
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}, "explanation": "ok"})
    router = ModelRouter([ModelInfo("m", "coding", available=True, provider=_Scripted(payload))])
    task = TaskEngine().add("t", "add")
    request = AgentRequest(task, TaskStatus.CODING, metadata={"approved": True})
    response = _coder(tmp_path, router=router).execute(request)
    assert response.success
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


class _Scripted:
    name = "scripted"

    def __init__(self, response):
        self.response = response

    def generate(self, prompt, *, context="", task="", instructions="", max_output_tokens=None, temperature=None):
        return ModelResult(self.response, self.name)
