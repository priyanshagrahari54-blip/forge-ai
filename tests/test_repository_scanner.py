from forge.intelligence.scanner import (
    RepositoryScanner,
)


def test_repository_scanner(tmp_path):
    (tmp_path / "app.py").write_text(
        "print('hello')",
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text(
        "# Test",
        encoding="utf-8",
    )

    (tmp_path / "node_modules").mkdir()

    (
        tmp_path
        / "node_modules"
        / "ignored.js"
    ).write_text(
        "ignored",
        encoding="utf-8",
    )

    result = RepositoryScanner(
        str(tmp_path)
    ).scan()

    paths = {
        file.path
        for file in result.files
    }

    assert "app.py" in paths
    assert "README.md" in paths
    assert "node_modules/ignored.js" not in paths
