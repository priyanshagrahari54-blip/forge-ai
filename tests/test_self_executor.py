from pathlib import Path
from forge.self_development import (
    ImprovementCandidate,
    ImprovementPriority,
    SelfDevelopmentExecutor,
)


def test_self_development_executor_flow(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "forge" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    executor = SelfDevelopmentExecutor(root=repo_dir)

    candidate = ImprovementCandidate(
        id="CANDIDATE-001",
        title="Add variable to mod.py",
        finding_id="FINDING-001",
        category="todo_fixme",
        priority=ImprovementPriority.LOW.value,
        description="Add y variable",
        proposed_improvement="Add y = 2 in mod.py",
        affected_files=["forge/mod.py"],
    )

    def modifier(c: ImprovementCandidate):
        p = repo_dir / "forge" / "mod.py"
        p.write_text("x = 1\ny = 2\n", encoding="utf-8")

    res = executor.execute_candidate(candidate, modifier_fn=modifier)

    assert res.accepted is True
    assert (repo_dir / "forge" / "mod.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    # Verify run history recorded
    history_files = list((repo_dir / ".forge" / "self" / "history").glob("run_*.json"))
    assert len(history_files) == 1
