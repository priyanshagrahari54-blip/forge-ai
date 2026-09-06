import json

from forge.agents.coder import CoderAgent
from forge.agents.debugger import DebuggerAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt, *, context="", task=""):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ModelResult(self.responses[index], self.name)


def coding_fabric(response_text):
    model = Model(name="scripted/model", provider="scripted",
                  capabilities=("coding", "debugging"))
    return ModelFabric(
        registry=ModelRegistry([model]),
        providers=ProviderRegistry({"scripted": ScriptedProvider([response_text])}),
    )


def coder_request(tmp_path, task="add a feature"):
    task = TaskEngine().add("task", task)
    return AgentRequest(task, TaskStatus.CODING, metadata={"approved": True})


def test_coder_agent_uses_fabric_to_write(tmp_path):
    payload = json.dumps({"changes": {"app.py": "def add(): return 1\n"}, "explanation": "done"})
    fabric = coding_fabric(payload)
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(coder_request(tmp_path))
    assert response.success is True
    assert (tmp_path / "app.py").read_text() == "def add(): return 1\n"
    assert response.metadata.get("model") == "scripted/model"
    # Provider-level feedback/telemetry was recorded by the fabric.
    assert fabric.router.history[0]["success"] is True
    assert fabric.telemetry.count("response") == 1


def test_coder_agent_fabric_still_enforces_validation(tmp_path):
    existing = tmp_path / "app.py"
    existing.write_text("original\n")
    unsafe = json.dumps({"changes": {"../outside.py": "x = 1\n"}})
    fabric = coding_fabric(unsafe)
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(coder_request(tmp_path))
    assert response.success is False
    assert existing.read_text() == "original\n"


def test_coder_agent_fabric_unavailable_capability(tmp_path):
    model = Model(name="seer", provider="scripted", capabilities=("vision",))
    fabric = ModelFabric(
        registry=ModelRegistry([model]),
        providers=ProviderRegistry({"scripted": ScriptedProvider(["ok"])}),
    )
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(coder_request(tmp_path))
    assert response.success is False
    assert "coding" in response.error


def test_debugger_agent_repairs_via_fabric(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def broken():\n    return 1\n")
    payload = json.dumps({"changes": {"app.py": "def fixed(): return 2\n"}, "explanation": "repair"})
    fabric = coding_fabric(payload)
    debugger = DebuggerAgent(str(tmp_path), fabric=fabric)
    changes = debugger.repair("fix", "assert failed", "", approved=True)
    assert changes == {"app.py": "def fixed(): return 2\n"}
    assert target.read_text() == "def fixed(): return 2\n"
    assert fabric.telemetry.count("response") == 1


def test_legacy_router_path_still_works(tmp_path):
    from forge.models.provider import MockProvider
    from forge.models.router import ModelInfo, ModelRouter

    payload = json.dumps({"changes": {"app.py": "x = 1\n"}, "explanation": "done"})
    router = ModelRouter([ModelInfo("contract", "coding", available=True,
                                    provider=MockProvider(payload))])
    response = CoderAgent(root=str(tmp_path), router=router).execute(coder_request(tmp_path))
    assert response.success is True
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


def test_self_development_executor_wires_fabric(tmp_path):
    from forge.self_development.executor import SelfDevelopmentExecutor

    fabric = ModelFabric.from_defaults()
    executor = SelfDevelopmentExecutor(root=str(tmp_path), fabric=fabric)
    assert executor.fabric is fabric
    assert executor.coder.fabric is fabric
    assert executor.router is None
