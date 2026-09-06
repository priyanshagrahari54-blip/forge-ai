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


def test_self_development_e2e_full_lifecycle(tmp_path: Path):
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

    # Setup repo structure
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "forge" / "core_module.py").write_text(
        "# TODO: optimize helper function\ndef helper(x):\n    return x + 1\n",
        encoding="utf-8",
    )

    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_core_module.py").write_text(
        "from forge.core_module import helper\n\ndef test_helper():\n    assert helper(2) == 3\n",
        encoding="utf-8",
    )

    # Initial git commit
    git = GitTool(repo=str(repo_dir))
    git.run("add", ".")
    git.run("commit", "-m", "Initial commit")

    # 1. Analyze repository
    analyzer = ForgeSelfAnalyzer(root=repo_dir)
    analysis = analyzer.analyze()
    findings = analysis["findings"]
    assert len(findings) >= 1

    # 2. Find known improvement & Generate candidate
    generator = ImprovementGenerator()
    candidates = generator.generate(findings)
    assert len(candidates) >= 1
    candidate = candidates[0]

    executor = SelfDevelopmentExecutor(root=repo_dir)

    # 3-8. Valid Improvement Cycle: Modify code correctly -> Test -> Evaluate -> Accept & Commit
    def valid_modifier(c: ImprovementCandidate):
        code_file = repo_dir / "forge" / "core_module.py"
        code_file.write_text(
            "# Optimized helper function\ndef helper(x):\n    return x + 1\n",
            encoding="utf-8",
        )

    valid_res = executor.execute_candidate(candidate, modifier_fn=valid_modifier)
    assert valid_res.accepted is True
    assert valid_res.rejection_reason is None

    # Check commit was created
    head_msg = git.run("log", "-1", "--pretty=%B").stdout.strip()
    assert "self-dev:" in head_msg

    # 9. Roll back broken improvement: Intentionally break code -> Test -> Evaluate -> Reject & Rollback
    def broken_modifier(c: ImprovementCandidate):
        code_file = repo_dir / "forge" / "core_module.py"
        code_file.write_text(
            "def helper(x):\n    raise RuntimeError('Intentionally broken!')\n",
            encoding="utf-8",
        )

    broken_res = executor.execute_candidate(candidate, modifier_fn=broken_modifier)
    assert broken_res.accepted is False
    assert broken_res.rejection_reason is not None

    # Verify code was rolled back after rejection
    content_after_rollback = (repo_dir / "forge" / "core_module.py").read_text(
        encoding="utf-8"
    )
    assert "Intentionally broken" not in content_after_rollback

    # 10. Persist history check
    history_files = list((repo_dir / ".forge" / "self" / "history").glob("run_*.json"))
    assert len(history_files) >= 2

    # Also test SelfDevelopmentLoop orchestration
    loop = SelfDevelopmentLoop(root=repo_dir)
    loop_results = loop.run(max_iterations=1, modifier_fn=valid_modifier)
    assert len(loop_results) == 1
