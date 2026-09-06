import json, subprocess
from forge.core.supervisor import Supervisor
from forge.tools.git import GitTool
from forge.models.provider import ModelResult
from forge.models.router import ModelInfo, ModelRouter

class AlwaysBrokenModel:
    name = "bounded-model"
    def generate(self, prompt, *, context="", task=""):
        return ModelResult(json.dumps({"changes": {"app.py": "def health(): return False\n"}, "explanation": "deliberately broken candidate"}), self.name)

class GoodModel:
    name = "good-model"
    def generate(self, prompt, *, context="", task=""):
        return ModelResult(json.dumps({"changes": {"app.py": "def health(): return True\n"}, "explanation": "preserve the passing implementation"}), self.name)

def repo(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import health\ndef test_health(): assert health()\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

def test_rejected_candidate_is_bounded_and_rolled_back(tmp_path):
    repo(tmp_path)
    (tmp_path / "unrelated.txt").write_text("keep me\n")
    model = AlwaysBrokenModel()
    router = ModelRouter([ModelInfo("bounded", "coding", available=True, provider=model, capabilities=("coding", "debugging"))])
    outcome = Supervisor("reject", tmp_path).run("Add a feature", approved=True, router=router, max_debug_retries=2)
    assert not outcome["accepted"]
    assert len(outcome["attempts"]) == 2
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert (tmp_path / "unrelated.txt").read_text() == "keep me\n"
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True, capture_output=True).stdout.count("initial") == 1
    assert all(".forge" not in line for line in subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True).stdout.splitlines())

def test_commit_failure_rolls_back_and_never_reports_acceptance(tmp_path, monkeypatch):
    repo(tmp_path)
    router = ModelRouter([ModelInfo("good", "coding", available=True, provider=GoodModel(), capabilities=("coding",))])
    def fail_commit(self, files, message):
        return subprocess.CompletedProcess(["git", "commit"], 1, "", "simulated commit failure")
    monkeypatch.setattr(GitTool, "commit_files", fail_commit)
    outcome = Supervisor("commit-failure", tmp_path).run("Preserve health", approved=True, router=router)
    assert not outcome["accepted"] and outcome["rollback"]
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True, capture_output=True).stdout.count("initial") == 1


def test_model_route_records_provider_failure_latency_and_success():
    class FailingProvider:
        name = "flaky"
        def generate(self, prompt, *, context="", task=""):
            raise RuntimeError("provider unavailable")
    router = ModelRouter([ModelInfo("flaky", "coding", available=True, provider=FailingProvider())])
    from forge.agents.coder import CoderAgent
    from forge.agents.execution import AgentRequest
    from forge.core.task_engine import TaskEngine, TaskStatus
    task = TaskEngine().add("route", "add feature")
    response = CoderAgent(root=".", router=router).execute(AgentRequest(task, TaskStatus.CODING, instructions="add feature", metadata={"approved": True}))
    assert not response.success
    assert router.history[-1]["success"] is False
    assert "latency" in router.history[-1]
