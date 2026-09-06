import subprocess
from pathlib import Path

from forge.security.permissions import PermissionLevel, PermissionManager
from forge.self_development import (
    CheckpointManager,
    HistoryStore,
    ImprovementCandidate,
    ImprovementPriority,
    SecurityScanner,
    SelfDevelopmentExecutor,
)
from forge.tools.git import GitTool


def test_safety_rollback_deletes_new_file(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), check=True)

    (repo_dir / "main.py").write_text("x = 1\n", encoding="utf-8")
    git = GitTool(repo=str(repo_dir))
    git.run("add", ".")
    git.run("commit", "-m", "init")

    executor = SelfDevelopmentExecutor(root=repo_dir)

    candidate = ImprovementCandidate(
        id="CANDIDATE-BAD",
        candidate_hash="badhash123",
        title="Bad candidate",
        finding_id="FINDING-001",
        category="todo_fixme",
        candidate_class="QUALITY_IMPROVEMENT",
        priority=ImprovementPriority.LOW.value,
        description="Bad modification",
        proposed_improvement="Add bad syntax file",
    )

    def bad_modifier(c):
        (repo_dir / "bad_syntax.py").write_text("def error(:\n", encoding="utf-8")

    res = executor.execute_candidate(candidate, modifier_fn=bad_modifier)

    assert res.accepted is False
    assert not (repo_dir / "bad_syntax.py").exists()
    assert (repo_dir / "main.py").read_text(encoding="utf-8") == "x = 1\n"


def test_safety_security_finding_rejection(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), check=True)

    (repo_dir / "app.py").write_text("print('start')\n", encoding="utf-8")
    git = GitTool(repo=str(repo_dir))
    git.run("add", ".")
    git.run("commit", "-m", "init")

    executor = SelfDevelopmentExecutor(root=repo_dir)

    candidate = ImprovementCandidate(
        id="CANDIDATE-SEC",
        candidate_hash="sechash123",
        title="Secret candidate",
        finding_id="FINDING-002",
        category="security",
        candidate_class="SECURITY_IMPROVEMENT",
        priority=ImprovementPriority.HIGH.value,
        description="Add hardcoded key",
        proposed_improvement="Add key to app.py",
    )

    def secret_modifier(c):
        (repo_dir / "app.py").write_text('api_key = "1234567890abcdef"\n', encoding="utf-8")

    res = executor.execute_candidate(candidate, modifier_fn=secret_modifier)

    assert res.accepted is False
    assert "security" in res.rejection_reason.lower() or "vulnerabilities" in res.rejection_reason.lower()
    assert "1234567890abcdef" not in (repo_dir / "app.py").read_text(encoding="utf-8")


def test_safety_repeated_failure_tracking(tmp_path: Path):
    store = HistoryStore(root=tmp_path)
    cand_hash = "repeated_fail_hash"

    assert not store.is_candidate_repeatedly_failed(cand_hash)
