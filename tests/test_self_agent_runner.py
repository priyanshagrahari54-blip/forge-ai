from pathlib import Path
from forge.self_development import AutonomousAgentRunner, SelfDevelopmentTask


def test_autonomous_agent_runner_edit(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "mod.py").write_text("# TODO: resolve this\nx = 1\n", encoding="utf-8")

    task = SelfDevelopmentTask(
        candidate_id="CANDIDATE-123",
        candidate_hash="hash123",
        objective="Resolve TODO comment",
        category="todo_fixme",
        affected_files=["forge/mod.py"],
    )

    runner = AutonomousAgentRunner(root=repo_dir)
    res = runner.run_modification(task)

    assert "forge/mod.py" in res["files_modified"]
    assert (repo_dir / "forge" / "mod.py").read_text(encoding="utf-8") == "# Resolved comment\nx = 1\n"
