import subprocess
from pathlib import Path

from forge.self_development import (
    ForgeSelfAnalyzer,
    ImprovementCandidate,
    ImprovementGenerator,
    SelfDevelopmentExecutor,
    SelfDevelopmentLoop,
)
from forge.tools.git import GitTool


def test_self_development_e2e_autonomous_and_rollback(tmp_path: Path):
    repo_dir = tmp_path / "forge_repo"
    repo_dir.mkdir()

    # Initialize git repo in temporary folder
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@forge.ai"], cwd=str(repo_dir), check=True
    )

    # Setup repo structure with TODO comment
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "forge" / "core_module.py").write_text(
        "# TODO: resolve helper function\ndef helper(x):\n    return x + 1\n",
        encoding="utf-8",
    )

    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_core_module.py").write_text(
        "from forge.core_module import helper\n\ndef test_helper():\n    assert helper(2) == 3\n",
        encoding="utf-8",
    )

    git = GitTool(repo=str(repo_dir))
    git.run("add", ".")
    git.run("commit", "-m", "Initial commit")

    # 1. Analyze repository
    analyzer = ForgeSelfAnalyzer(root=repo_dir)
    analysis = analyzer.analyze()
    findings = analysis["findings"]
    assert len(findings) >= 1

    # 2. Generate candidate with SHA-256 hash identity
    generator = ImprovementGenerator()
    candidates = generator.generate(findings)
    assert len(candidates) >= 1
    candidate = candidates[0]
    assert len(candidate.candidate_hash) == 12

    executor = SelfDevelopmentExecutor(root=repo_dir)

    # 3. Autonomous AI-Driven Execution (No manual modifier_fn)
    auto_res = executor.execute_candidate(candidate)
    assert auto_res.accepted is True
    assert auto_res.rejection_reason is None

    # Verify autonomous solver resolved TODO comment and committed changes
    code_content = (repo_dir / "forge" / "core_module.py").read_text(encoding="utf-8")
    assert "# TODO" not in code_content
    head_msg = git.run("log", "-1", "--pretty=%B").stdout.strip()
    assert "self-dev:" in head_msg

    # 4. Intentionally Bad Candidate -> Rejected & Rolled Back
    bad_candidate = ImprovementCandidate(
        id="CANDIDATE-BROKEN",
        candidate_hash="brokenhash1",
        title="Broken candidate",
        finding_id="FINDING-999",
        category="todo_fixme",
        candidate_class="QUALITY_IMPROVEMENT",
        priority="high",
        description="Introduce syntax error",
        proposed_improvement="Syntax error in core_module.py",
        affected_files=["forge/core_module.py"],
    )

    def broken_modifier(c):
        code_file = repo_dir / "forge" / "core_module.py"
        code_file.write_text("def helper(x:\n    return x\n", encoding="utf-8")

    broken_res = executor.execute_candidate(bad_candidate, modifier_fn=broken_modifier)
    assert broken_res.accepted is False
    assert broken_res.rejection_reason is not None

    # Verify rollback restored exact state
    content_after_rollback = (repo_dir / "forge" / "core_module.py").read_text(
        encoding="utf-8"
    )
    assert "def helper(x:\n" not in content_after_rollback

    # 5. Verify reproducible history
    history_files = list((repo_dir / ".forge" / "self" / "history").glob("run_*.json"))
    assert len(history_files) >= 2
