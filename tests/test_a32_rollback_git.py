"""Rollback scope and safe-staging regression tests (A32 hardening).

Rollback is candidate-scoped and never runs broad destructive Git commands;
autonomous commits contain exactly the accepted ChangeSet files.
"""
import json
import subprocess

import pytest

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.tools.checkpoint import CheckpointManager
from forge.tools.git import GitTool


def _repo(root):
    (root / "app.py").write_text("def health(): return True\n")
    (root / "README.md").write_text("# Demo\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "README.md", "app.py", "tests/test_app.py"],
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
            Model(name="m/a32", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


CSV_PAYLOAD = json.dumps({
    "summary": "Add CSV export",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": "def health(): return True\n\ndef export_csv():\n    return 'x'\n"},
        {"path": "tests/test_csv.py", "action": "create",
         "content": "from app import export_csv\ndef test_csv():\n    assert export_csv() == 'x'\n"},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "added export",
    "risk_level": "low",
})

BROKEN_PAYLOAD = json.dumps(
    {"changes": {"app.py": "def health(): return False\n"}})


def test_rollback_never_runs_broad_destructive_git_commands(tmp_path, monkeypatch):
    _repo(tmp_path)
    calls = []
    original_run = GitTool.run

    def spy(self, *args):
        calls.append(args)
        assert args[:2] != ("reset", "--hard"), "broad reset is forbidden"
        assert args[:2] != ("clean", "-fd"), "broad clean is forbidden"
        if args and args[0] == "add":
            assert args != ("add", "."), "broad staging is forbidden"
            assert args != ("add", "-A"), "broad staging is forbidden"
            assert "--all" not in args, "broad staging is forbidden"
        return original_run(self, *args)

    monkeypatch.setattr(GitTool, "run", spy)
    outcome = Supervisor("rollback-spy", tmp_path).run(
        "Add a feature", approved=True, fabric=_fabric(ScriptedLoop(BROKEN_PAYLOAD)),
        max_debug_retries=1)

    assert not outcome["accepted"]
    assert outcome["rollback"] is True
    assert calls  # Git was used (status/diff), only ever narrowly
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"


def test_autonomous_commit_excludes_unrelated_tracked_modification(tmp_path):
    _repo(tmp_path)
    (tmp_path / "README.md").write_text("# Demo\n\nUser edits.\n")
    outcome = Supervisor("commit-scope", tmp_path).run(
        "Add CSV export functionality and tests", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)))

    assert outcome["accepted"]
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True).stdout.splitlines()
    assert committed == ["app.py", "tests/test_csv.py"]
    # The unrelated tracked modification survives, uncommitted, in the worktree.
    assert (tmp_path / "README.md").read_text() == "# Demo\n\nUser edits.\n"
    head_readme = subprocess.run(
        ["git", "show", "HEAD:README.md"], cwd=tmp_path, text=True,
        capture_output=True).stdout
    assert head_readme == "# Demo\n"
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True,
                            capture_output=True).stdout
    assert "README.md" in status


def test_commit_refused_when_index_holds_unrelated_staged_file(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")
    subprocess.run(["git", "add", "--", "notes.txt"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 2\n")

    with pytest.raises(RuntimeError, match="Staged file set mismatch"):
        GitTool(tmp_path).commit_accepted(
            ["app.py"], "should not commit", {"accepted": True})

    staged = GitTool(tmp_path).run("diff", "--cached", "--name-only").stdout.splitlines()
    assert staged == ["notes.txt"]  # candidate unstaged; user index untouched
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True,
                          capture_output=True).stdout.count("initial") == 1


def test_checkpoint_rollback_restores_deleted_candidate_only(tmp_path):
    (tmp_path / "victim.py").write_text("original\n")
    (tmp_path / "user.txt").write_text("user before\n")
    manager = CheckpointManager(tmp_path)
    checkpoint = manager.create("delete-scope")

    (tmp_path / "victim.py").unlink()
    (tmp_path / "user.txt").write_text("user after\n")
    (tmp_path / "user_new.txt").write_text("untracked user work\n")

    manager.rollback(checkpoint, ["victim.py"])
    assert (tmp_path / "victim.py").read_text() == "original\n"
    assert (tmp_path / "user.txt").read_text() == "user after\n"
    assert (tmp_path / "user_new.txt").read_text() == "untracked user work\n"
