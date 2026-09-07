"""Supervisor end-to-end through the centralized Model Fabric.

Mirrors the legacy router E2E, but every model call is routed and recorded by
the fabric: capability routing, provider call, telemetry, and feedback.
"""
import json
import subprocess

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


class RepairingModel:
    name = "fabric-repair-model"

    def __init__(self):
        self.calls = []

    def generate(self, prompt, *, context="", task=""):
        self.calls.append(prompt)
        if "FAILURE:" in prompt:
            return ModelResult(json.dumps({
                "changes": {"app.py": "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"},
                "explanation": "Corrected the failing CSV output.",
            }), self.name)
        return ModelResult(json.dumps({
            "changes": {
                "app.py": "def export_csv():\n    return 'name,score'\n",
                "tests/test_csv.py": "from app import export_csv\ndef test_csv_export():\n    assert export_csv() == 'name,score\\nAda,3\\n'\n",
            },
            "explanation": "Added CSV export and a regression test.",
        }), self.name)


def test_supervisor_runs_end_to_end_through_fabric(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import health\ndef test_health(): assert health()\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "notes.txt").write_text("user work\n")

    provider = RepairingModel()
    fabric = ModelFabric(
        registry=ModelRegistry([Model(
            name="fabric/model", provider="fabric-provider",
            capabilities=("coding", "debugging"), free=True, local=True,
        )]),
        providers=ProviderRegistry({"fabric-provider": provider}),
    )

    outcome = Supervisor("isolated", tmp_path).run(
        "Add CSV export functionality and tests", approved=True, fabric=fabric,
    )

    assert outcome["accepted"] is True
    assert "ROLLBACK" not in outcome["stages"]
    assert outcome["attempts"], "first model implementation must fail and trigger repair"
    assert len(provider.calls) >= 2
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True).stdout
    assert "notes.txt" in status and ".forge" not in status
    committed = subprocess.run(["git", "show", "--format=", "--name-only", "HEAD"], cwd=tmp_path, text=True, capture_output=True).stdout.splitlines()
    assert committed == ["app.py", "tests/test_csv.py"]

    # The fabric recorded routing + provider outcomes, not the legacy router.
    assert fabric.telemetry.count("route") >= 2
    assert fabric.telemetry.count("response") >= 2
    assert any(event["success"] for event in fabric.router.history)
