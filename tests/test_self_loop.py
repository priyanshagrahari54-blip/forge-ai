from pathlib import Path
from forge.self_development import ImprovementCandidate, SelfDevelopmentLoop


def test_self_development_loop(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "forge").mkdir()
    (repo_dir / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "forge" / "sample.py").write_text(
        "# TODO: add implementation\ndef dummy(): pass\n", encoding="utf-8"
    )

    loop = SelfDevelopmentLoop(root=repo_dir)

    def modifier(c: ImprovementCandidate):
        p = repo_dir / "forge" / "sample.py"
        p.write_text("# TODO: add implementation\ndef dummy(): return 42\n", encoding="utf-8")

    results = loop.run(max_iterations=1, modifier_fn=modifier)

    assert len(results) == 1
    assert results[0].accepted is True

    st = loop.status()
    assert st["iteration_count"] == 1
    assert st["accepted_runs"] == 1
