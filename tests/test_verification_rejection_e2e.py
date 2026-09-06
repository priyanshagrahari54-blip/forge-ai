import json, subprocess
import pytest
from forge.core.supervisor import Supervisor
from forge.models.provider import ModelResult
from forge.models.router import ModelInfo, ModelRouter

class OneFileModel:
    name = "gate-model"
    def __init__(self, source): self.source = source
    def generate(self, prompt, *, context="", task=""):
        return ModelResult(json.dumps({"changes": {"app.py": self.source}}), self.name)

def make_repo(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import health\ndef test_health(): assert health()\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

@pytest.mark.parametrize("source,failed_gate", [
    ("def health(): return True\napi_key = 'hardcoded-secret-value'\n", "security"),
    ("def health(): return True\nvalue = eval('1')\n", "review"),
])
def test_acceptance_cannot_bypass_security_or_review(tmp_path, source, failed_gate):
    make_repo(tmp_path)
    router = ModelRouter([ModelInfo("gate", "coding", available=True, provider=OneFileModel(source), capabilities=("coding", "debugging"))])
    outcome = Supervisor("gates", tmp_path).run("Add a feature", approved=True, router=router)
    assert not outcome["accepted"]
    assert any(not gate["passed"] and gate["name"] == failed_gate for gate in outcome["gates"])
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True, capture_output=True).stdout.count("initial") == 1
