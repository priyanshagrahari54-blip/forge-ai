import subprocess
from pathlib import Path
from forge.self_development import CheckpointManager
from forge.tools.git import GitTool


def test_checkpoint_and_rollback_new_file(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), check=True)

    (repo_dir / "existing.txt").write_text("v1\n", encoding="utf-8")
    git = GitTool(repo=str(repo_dir))
    git.run("add", ".")
    git.run("commit", "-m", "init")

    ckpt_mgr = CheckpointManager(root=repo_dir, git_tool=git)
    snapshot = ckpt_mgr.create_checkpoint("ckpt-1")

    # Modify existing file & add new file
    (repo_dir / "existing.txt").write_text("v2 modified\n", encoding="utf-8")
    new_file = repo_dir / "new_created_file.py"
    new_file.write_text("print('hello')\n", encoding="utf-8")

    assert new_file.exists()
    assert (repo_dir / "existing.txt").read_text(encoding="utf-8") == "v2 modified\n"

    # Execute rollback
    ckpt_mgr.rollback(snapshot)

    # Verify rollback restored existing.txt and deleted new_created_file.py
    assert not new_file.exists()
    assert (repo_dir / "existing.txt").read_text(encoding="utf-8") == "v1\n"
