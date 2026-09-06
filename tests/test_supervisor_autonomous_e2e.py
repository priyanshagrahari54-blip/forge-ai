import json, subprocess
from pathlib import Path
from forge.core.supervisor import Supervisor
from forge.models.provider import ModelResult
from forge.models.router import ModelInfo, ModelRouter

class RepairingModel:
    name = "isolated-repair-model"
    def __init__(self): self.calls = []
    def generate(self, prompt, *, context="", task=""):
        self.calls.append(prompt)
        if "FAILURE:" in prompt:
            # This is a genuine second model response driven by pytest output.
            return ModelResult(json.dumps({"changes": {"app.py": "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"}, "explanation": "Corrected the failing CSV output while preserving health."}), self.name)
        return ModelResult(json.dumps({"changes": {
            "app.py": "def export_csv():\n    return 'name,score'\n",
            "tests/test_csv.py": "from app import export_csv\ndef test_csv_export():\n    assert export_csv() == 'name,score\\nAda,3\\n'\n",
        }, "explanation": "Added CSV export and a regression test."}), self.name)

def test_supervisor_runs_model_repair_and_commits_in_isolated_repo(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import health\ndef test_health(): assert health()\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", "app.py", "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    provider = RepairingModel()
    router = ModelRouter([ModelInfo("repair-model", "coding", available=True, free=True, provider=provider, capabilities=("coding", "debugging"))])
    outcome = Supervisor("isolated", tmp_path).run("Add CSV export functionality", approved=True, router=router)

    assert outcome["accepted"] is True
    assert "ROLLBACK" not in outcome["stages"]
    assert outcome["attempts"], "the first model implementation must fail and trigger repair"
    assert len(provider.calls) >= 2
    assert any("ERROR" in call or "FAILED" in call or "AssertionError" in call for call in provider.calls[1:])
    assert "COMMIT" in outcome["stages"]
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"
    assert subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True).stdout.strip() == ""
