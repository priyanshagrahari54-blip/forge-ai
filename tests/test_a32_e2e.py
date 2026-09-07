"""A32 autonomous-engineering end-to-end tests.

These drive the complete orchestration path through the Model Fabric and
inspect the resulting repository — never the final answer only.
"""
import json
import subprocess

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


def _repo(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)


class CleanModel:
    """Deterministic provider that behaves like a model through the full loop."""

    name = "clean"

    def __init__(self):
        self.prompts = []

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        self.prompts.append(prompt)
        if "Review the following change set" in prompt:
            return ModelResult(json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        return ModelResult(json.dumps({
            "summary": "Add CSV export",
            "changes": [
                {"path": "app.py", "action": "modify",
                 "content": "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"},
                {"path": "tests/test_csv.py", "action": "create",
                 "content": "from app import export_csv\ndef test_csv_export():\n    assert export_csv() == 'name,score\\nAda,3\\n'\n"},
            ],
            "tests": ["tests/test_csv.py"],
            "reasoning_summary": "added an export function and a regression test",
            "risks": [],
        }), self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a32", provider="p", capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def test_a32_autonomous_task_full_loop(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")

    provider = CleanModel()
    outcome = Supervisor("a32", tmp_path).run(
        "Add CSV export functionality and tests", approved=True, fabric=_fabric(provider),
    )

    assert outcome["accepted"] is True
    for stage in ("PLAN", "AGENTS", "MODEL", "CODE", "TEST", "REVIEW", "SECURITY", "ACCEPTANCE", "CHECKPOINT", "COMMIT", "COMPLETED"):
        assert stage in outcome["stages"], f"missing stage {stage}"
    assert "ROLLBACK" not in outcome["stages"]

    # Structured review + acceptance decisions are recorded.
    assert outcome["review"]["verdict"] == "APPROVE"
    assert outcome["acceptance"]["accepted"] is True
    assert outcome["acceptance"]["risk_level"] == "NONE"

    # The repository was genuinely modified and only the touched files committed.
    assert "export_csv" in (tmp_path / "app.py").read_text()
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True,
    ).stdout.splitlines()
    assert committed == ["app.py", "tests/test_csv.py"]
    assert "notes.txt" in subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True).stdout

    # Structured observability report is present and complete.
    report = outcome["report"]
    assert report["task_id"].startswith("supervisor-task-")
    assert report["trace_id"] == outcome["run_id"]
    assert report["final_status"] == "COMPLETED"
    assert report["checkpoint_id"]
    assert report["files_changed"] == ["app.py", "tests/test_csv.py"]
    assert report["files_read"]
    assert report["commands_run"]
    assert report["acceptance"]["accepted"] is True
    assert "API key" not in json.dumps(report)


class FailingThenFixingModel:
    """First implementation fails; the first repair repeats the mistake and only
    the second repair — which receives PREVIOUS ATTEMPTS — fixes it."""

    name = "fixing"

    def __init__(self):
        self.prompts = []

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        self.prompts.append(prompt)
        if "FAILURE:" in prompt:
            if "PREVIOUS ATTEMPTS" in prompt:
                return ModelResult(json.dumps({
                    "changes": {"app.py": "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"},
                    "explanation": "Corrected the failing CSV output.",
                }), self.name)
            # First repair repeats the broken strategy.
            return ModelResult(json.dumps({
                "changes": {"app.py": "def export_csv():\n    return 'name,score'\n"},
                "explanation": "retry the same wrong fix",
            }), self.name)
        if "Review the following change set" in prompt:
            return ModelResult(json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        # First implementation is deliberately incorrect.
        return ModelResult(json.dumps({
            "changes": [
                {"path": "app.py", "action": "modify",
                 "content": "def export_csv():\n    return 'name,score'\n"},
                {"path": "tests/test_csv.py", "action": "create",
                 "content": "from app import export_csv\ndef test_csv_export():\n    assert export_csv() == 'name,score\\nAda,3\\n'\n"},
            ],
        }), self.name)


def test_a32_failure_recovery_repairs_then_accepts(tmp_path):
    _repo(tmp_path)
    provider = FailingThenFixingModel()
    outcome = Supervisor("a32-repair", tmp_path).run(
        "Add CSV export functionality and tests", approved=True, fabric=_fabric(provider),
    )

    assert outcome["accepted"] is True
    # Two failing attempts (initial + first wrong repair), then the passing
    # retest after the second repair used the prior-attempt context.
    assert len(outcome["attempts"]) == 3
    assert outcome["attempts"][-1]["test_passed"] is True
    assert "DEBUG" in outcome["stages"] and "REPAIR" in outcome["stages"]
    # The second repair prompt carries prior-attempt diagnostics so the model changes strategy.
    assert any("PREVIOUS ATTEMPTS" in prompt for prompt in provider.prompts)
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n\ndef export_csv():\n    return 'name,score\\nAda,3\\n'\n"
    assert outcome["report"]["final_status"] == "COMPLETED"
    assert outcome["report"]["retries"] == len(outcome["attempts"])


class AlwaysBrokenModel:
    name = "broken"

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            return ModelResult(json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        return ModelResult(json.dumps({
            "changes": {"app.py": "def health(): return False\n"},
            "explanation": "deliberately broken candidate",
        }), self.name)


def test_a32_permanent_failure_rolls_back_and_reports(tmp_path):
    _repo(tmp_path)
    (tmp_path / "unrelated.txt").write_text("keep me\n")

    outcome = Supervisor("a32-fail", tmp_path).run(
        "Add a feature", approved=True, fabric=_fabric(AlwaysBrokenModel()), max_debug_retries=2,
    )

    assert not outcome["accepted"]
    assert outcome["rollback"] is True
    assert len(outcome["attempts"]) == 2
    # Repository restored exactly; unrelated work preserved; nothing committed.
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert (tmp_path / "unrelated.txt").read_text() == "keep me\n"
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True, capture_output=True).stdout.count("initial") == 1
    assert not (tmp_path / "tests" / "test_csv.py").exists()

    report = outcome["report"]
    assert report["final_status"] == "FAILED"
    assert report["rollback"] is True
    assert report["error"]
    assert report["checkpoint_id"]
