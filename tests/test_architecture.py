from pathlib import Path

from forge.intelligence.architecture import (
    ArchitectureAnalyzer,
    generate_architecture_report,
)


def create_project(root: Path) -> None:
    (root / ".gitignore").write_text(
        "__pycache__/\n.venv/\nbuild/\n",
        encoding="utf-8",
    )

    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n",
        encoding="utf-8",
    )

    (root / "app.py").write_text(
        "def main():\n    pass\n",
        encoding="utf-8",
    )

    package = root / "forge"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(
        "def run():\n    pass\n",
        encoding="utf-8",
    )

    tests = root / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text(
        "def test_run():\n    pass\n",
        encoding="utf-8",
    )

    ignored = root / "__pycache__"
    ignored.mkdir()
    (ignored / "ignored.py").write_text(
        "def ignored():\n    pass\n",
        encoding="utf-8",
    )


def test_architecture_detection(tmp_path: Path):
    create_project(tmp_path)

    architecture = ArchitectureAnalyzer(tmp_path).analyze()

    assert "pyproject.toml" in architecture.config_files

    assert "app.py" in architecture.entry_points
    assert "forge/main.py" in architecture.entry_points

    assert "tests/test_main.py" in architecture.test_files

    assert "app.py" in architecture.source_files
    assert "forge/main.py" in architecture.source_files
    assert "tests/test_main.py" in architecture.source_files
    assert "__pycache__/ignored.py" not in architecture.source_files

    package_paths = {
        package.path
        for package in architecture.packages
    }

    assert "forge" in package_paths


def test_package_for_file(tmp_path: Path):
    create_project(tmp_path)

    architecture = ArchitectureAnalyzer(tmp_path).analyze()

    assert architecture.package_for_file("forge/main.py") == "forge"
    assert architecture.package_for_file("app.py") is None


def test_architecture_report(tmp_path: Path):
    create_project(tmp_path)

    architecture = ArchitectureAnalyzer(tmp_path).analyze()
    report = generate_architecture_report(architecture)

    assert "# Repository Architecture" in report
    assert "## Packages" in report
    assert "## Entry Points" in report
    assert "## Configuration" in report
    assert "## Tests" in report
    assert "`forge` (python_package)" in report
    assert "`pyproject.toml`" in report
    assert "`tests/test_main.py`" in report
