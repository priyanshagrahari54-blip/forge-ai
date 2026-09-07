"""Cockpit backend interface tests (A33)."""
import json
import subprocess

from forge.cockpit import CockpitService
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.security.policy import Resource


def _repo(root):
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


class ScriptedLoop:
    name = "scripted-loop"

    def __init__(self, coder_payload):
        self.coder_payload = coder_payload

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            return ModelResult(
                json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        return ModelResult(self.coder_payload, self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a33", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


PAYLOAD = json.dumps({
    "summary": "Bump",
    "changes": [{"path": "app.py", "action": "modify",
                 "content": "def health(): return True\n\nX = 1\n"}],
    "reasoning_summary": "bump",
    "risk_level": "low",
})


def test_submit_and_status_round_trip(tmp_path):
    _repo(tmp_path)
    service = CockpitService(str(tmp_path))
    submitted = service.submit_task(
        "Bump the app", approved=True, fabric=_fabric(ScriptedLoop(PAYLOAD)))
    assert submitted["status"] == "completed"
    status = service.task_status(submitted["task_id"])
    assert status["accepted"] is True
    assert status["files"] == ["app.py"]
    assert "COMMIT" in status["stages"]
    assert service.task_status("missing") is None
    assert service.list_tasks()[0]["task_id"] == submitted["task_id"]


def test_failed_task_reported_with_error(tmp_path):
    _repo(tmp_path)
    service = CockpitService(str(tmp_path))
    submitted = service.submit_task(
        "Bump the app", approved=False, fabric=_fabric(ScriptedLoop(PAYLOAD)))
    assert submitted["status"] == "failed"
    logs = service.task_logs(submitted["task_id"])
    assert logs["rollback"] is True
    assert logs["error"]
    assert service.task_logs("missing") == {}
    assert service.task_events("missing") == []


def test_approval_queue_decide_and_mint(tmp_path):
    service = CockpitService(str(tmp_path))
    request_id = service.request_approval(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("src/a.py",), reason="feature")
    assert len(service.permission_requests()) == 1
    decided = service.decide_approval(request_id, True, "operator")
    assert decided["status"] == "approved"
    assert service.permission_requests() == []
    assert len(service.permission_requests(status="all")) == 1
    token = service.mint_token(request_id, "operator")
    assert token["agent"] == "coder"
    assert token["scopes"] == ["src/a.py"]


def test_event_stream_and_task_events(tmp_path):
    _repo(tmp_path)
    service = CockpitService(str(tmp_path))
    submitted = service.submit_task(
        "Bump the app", approved=True, fabric=_fabric(ScriptedLoop(PAYLOAD)))
    task_id = submitted["task_id"]
    events = list(service.event_stream(task_id))
    assert events
    assert all(item["task_id"] == task_id for item in events)
    assert all("decision" in item for item in events)
    report_events = service.task_events(task_id)
    assert report_events[0]["name"] == "task_started"


def test_model_and_agent_status(tmp_path):
    _repo(tmp_path)
    service = CockpitService(str(tmp_path))
    assert service.model_status() == {"runs": 0, "models": {}}
    service.submit_task("Bump the app", approved=True,
                        fabric=_fabric(ScriptedLoop(PAYLOAD)))
    models = service.model_status()
    assert models["runs"] == 1 and models["models"] == {"m/a33": 1}
    agents = service.agent_status()
    assert agents["runs"] == 1 and "coder" in agents["agents"]
