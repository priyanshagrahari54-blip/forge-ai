"""Autonomous-engineering failure-matrix E2E tests (A32.12 rebuild).

Each scenario drives a full ``Supervisor.run`` through the Model Fabric with
a scripted provider and proves the end-to-end behavior: rejection, exact
rollback, no commit, unrelated-work preservation, and structured reporting.
"""
import json
import subprocess

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.tools.git import GitTool


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
    """Deterministic provider routing coder/reviewer/repair prompts."""

    name = "scripted-loop"

    def __init__(self, coder_payload, repair_payload="{}"):
        self.coder_payload = coder_payload
        self.repair_payload = repair_payload

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            return ModelResult(
                json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        if "FAILURE:" in prompt:
            return ModelResult(self.repair_payload, self.name)
        return ModelResult(self.coder_payload, self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a32", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def _run(root, provider, **kwargs):
    return Supervisor("matrix", root).run(
        "Add a feature", approved=True, fabric=_fabric(provider), **kwargs)


def _commits(root):
    return subprocess.run(["git", "log", "--oneline"], cwd=root, text=True,
                          capture_output=True).stdout


def _assert_rejected_cleanly(outcome, root, *, commit_attempted=False):
    assert not outcome["accepted"]
    assert outcome["rollback"] is True
    assert "ROLLBACK" in outcome["stages"]
    if commit_attempted:
        assert "COMMIT" in outcome["stages"]
    else:
        assert "COMMIT" not in outcome["stages"]
    assert (root / "app.py").read_text() == "def health(): return True\n"
    assert _commits(root).count("initial") == 1
    assert GitTool(root).run("diff", "--cached", "--name-only").stdout == ""
    report = outcome["report"]
    assert report["final_status"] == "FAILED"
    assert report["rollback"] is True
    assert report["error"]
    assert report["checkpoint_id"]


def test_malformed_model_output_rolls_back_without_commit(tmp_path):
    _repo(tmp_path)
    outcome = _run(tmp_path, ScriptedLoop("not json at all"))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert "JSON" in outcome["error"]
    assert "acceptance" not in outcome  # rejected before any gate ran


def test_unauthorized_forge_path_rejected(tmp_path):
    _repo(tmp_path)
    payload = json.dumps({"changes": {".forge/state.json": "x = 1\n"}})
    outcome = _run(tmp_path, ScriptedLoop(payload))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert not (tmp_path / ".forge" / "state.json").exists()


def test_traversal_attempt_rejected(tmp_path):
    _repo(tmp_path)
    payload = json.dumps({"changes": {"../a32_matrix_evil.py": "x = 1\n"}})
    outcome = _run(tmp_path, ScriptedLoop(payload))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert not (tmp_path.parent / "a32_matrix_evil.py").exists()


def test_secret_content_rejected_before_write(tmp_path):
    _repo(tmp_path)
    payload = json.dumps({"changes": {
        "app.py": "api_key = 'sup3r-secret-value'\ndef health(): return True\n"}})
    outcome = _run(tmp_path, ScriptedLoop(payload))
    _assert_rejected_cleanly(outcome, tmp_path)


def test_credential_filename_rejected(tmp_path):
    _repo(tmp_path)
    payload = json.dumps({"changes": {"credentials.json": "{}"}})
    outcome = _run(tmp_path, ScriptedLoop(payload))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert not (tmp_path / "credentials.json").exists()


def test_repair_failure_rolls_back_with_recorded_attempts(tmp_path):
    _repo(tmp_path)
    coder = json.dumps({"changes": {"app.py": "def health(): return False\n"}})
    outcome = _run(tmp_path, ScriptedLoop(coder, repair_payload="not json"),
                   max_debug_retries=2)
    _assert_rejected_cleanly(outcome, tmp_path)
    assert len(outcome["attempts"]) == 1
    assert "repair failed" in outcome["attempts"][0]["reason"]
    assert "DEBUG" in outcome["stages"] and "REPAIR" in outcome["stages"]


def test_review_rejection_rolls_back_and_names_failed_gate(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")
    coder = json.dumps({"changes": {
        "app.py": "def health(): return True\ndef todo():\n    pass\n"}})
    outcome = _run(tmp_path, ScriptedLoop(coder))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert outcome["review"]["verdict"] == "BLOCK"
    assert outcome["acceptance"]["failed_gates"] == ["review"]
    assert (tmp_path / "notes.txt").read_text() == "user work\n"


def test_security_rejection_rolls_back_and_names_failed_gate(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")
    coder = json.dumps({"changes": {
        "app.py": "def health(): return True\nAWS_KEY = 'AKIA1234567890ABCDEF'\n"}})
    outcome = _run(tmp_path, ScriptedLoop(coder))
    _assert_rejected_cleanly(outcome, tmp_path)
    assert outcome["acceptance"]["security_result"] == "FAIL"
    assert outcome["acceptance"]["failed_gates"] == ["security"]
    assert (tmp_path / "notes.txt").read_text() == "user work\n"


def test_commit_failure_rolls_back_on_fabric_path(tmp_path, monkeypatch):
    _repo(tmp_path)
    coder = json.dumps({"changes": [
        {"path": "app.py", "action": "modify",
         "content": "def health(): return True\n\ndef extra():\n    return 1\n"},
        {"path": "tests/test_extra.py", "action": "create",
         "content": "from app import extra\ndef test_extra():\n    assert extra() == 1\n"},
    ], "tests_to_run": ["tests/test_extra.py"]})

    def fail_commit(self, files, message):
        return subprocess.CompletedProcess(["git", "commit"], 1, "", "boom")

    monkeypatch.setattr(GitTool, "commit_files", fail_commit)
    outcome = _run(tmp_path, ScriptedLoop(coder))
    _assert_rejected_cleanly(outcome, tmp_path, commit_attempted=True)
    assert not (tmp_path / "tests" / "test_extra.py").exists()
    assert outcome["acceptance"]["accepted"] is True  # accepted, then commit failed
