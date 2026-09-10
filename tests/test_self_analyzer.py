from pathlib import Path
from forge.self_development import ForgeSelfAnalyzer, Finding, FindingCategory


def test_forge_self_analyzer(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "forge" / "sample.py").write_text(
        "# TODO: fix this function\ndef foo():\n    pass\n",
        encoding="utf-8",
    )
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_sample.py").write_text(
        "def test_foo(): assert True\n",
        encoding="utf-8",
    )

    analyzer = ForgeSelfAnalyzer(root=repo_dir)
    res = analyzer.analyze()

    assert "findings" in res
    assert "repo_metrics" in res
    assert (repo_dir / ".forge" / "self" / "analysis.json").exists()

    findings = [Finding.from_dict(f) for f in res["findings"]]
    assert len(findings) > 0
    todo_findings = [f for f in findings if f.category == FindingCategory.TODO_FIXME.value]
    assert len(todo_findings) >= 1
    assert todo_findings[0].affected_files == ["forge/sample.py"]
