import json
from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.provider import MockProvider
from forge.models.router import ModelInfo, ModelRouter


def request(tmp_path):
    task = TaskEngine().add("contract", "add a feature")
    return AgentRequest(task, TaskStatus.CODING, metadata={"approved": True})


def agent(tmp_path, response):
    provider = MockProvider(response)
    router = ModelRouter([ModelInfo("contract", "coding", available=True, provider=provider)])
    return CoderAgent(root=str(tmp_path), router=router)


def test_invalid_model_response_does_not_write(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("original\n")
    response = agent(tmp_path, "not json").execute(request(tmp_path))
    assert not response.success
    assert target.read_text() == "original\n"


def test_model_paths_and_secrets_are_rejected_before_any_write(tmp_path):
    existing = tmp_path / "app.py"
    existing.write_text("original\n")
    for path in ("/tmp/out.py", "../out.py", ".forge/state.json"):
        payload = json.dumps({"changes": {path: "x = 1\n"}})
        response = agent(tmp_path, payload).execute(request(tmp_path))
        assert not response.success
        assert existing.read_text() == "original\n"
    secret = json.dumps({"changes": {"app.py": "api_key = 'hardcoded-secret-value'\n"}})
    assert not agent(tmp_path, secret).execute(request(tmp_path)).success
    assert existing.read_text() == "original\n"


def test_unavailable_model_fails_safely(tmp_path):
    router = ModelRouter([ModelInfo("offline", "coding", available=False)])
    response = CoderAgent(root=str(tmp_path), router=router).execute(request(tmp_path))
    assert not response.success
    assert "No available coding model" in response.error
    assert not list(tmp_path.iterdir())
