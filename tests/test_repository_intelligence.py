from pathlib import Path

from forge.intelligence.repository import RepositoryIntelligence


def create_project(root: Path) -> None:
    (root / ".gitignore").write_text(
        "__pycache__/\n",
        encoding="utf-8",
    )

    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n",
        encoding="utf-8",
    )

    package = root / "demo"
    package.mkdir()

    (package / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    (package / "models.py").write_text(
        """
class User:
    pass
""",
        encoding="utf-8",
    )

    (package / "service.py").write_text(
        """
from demo.models import User

def run():
    return User()
""",
        encoding="utf-8",
    )

    (package / "__main__.py").write_text(
        """
from demo.service import run

def main():
    return run()

if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )

    tests = root / "tests"
    tests.mkdir()

    (tests / "test_service.py").write_text(
        """
from demo.service import run

def test_run():
    run()
""",
        encoding="utf-8",
    )


def test_builds_unified_intelligence(tmp_path: Path):
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    assert intelligence.root == tmp_path.resolve()
    assert len(intelligence.symbols.symbols) > 0
    assert len(intelligence.dependencies.dependencies) > 0
    assert len(intelligence.architecture.source_files) == 5
    assert len(intelligence.architecture.test_files) == 1
    assert "demo" in intelligence.architecture.entry_points[0]
    assert "python" in intelligence.runtime.project_type


def test_source_context(tmp_path: Path):
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    context = intelligence.source_context("demo/service.py")

    assert context["source"] == "demo/service.py"
    assert any(
        symbol["name"] == "run"
        for symbol in context["symbols"]
    )
    assert "demo/models.py" in context["dependencies"]
    assert context["package"] == "demo"


def test_impact_and_tests(tmp_path: Path):
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    assert intelligence.impact("demo/models.py") == [
        "demo/service.py",
        "demo/__main__.py",
    ]

    assert intelligence.affected_tests(
        "demo/service.py"
    ) == ["tests/test_service.py"]


def test_summary(tmp_path: Path):
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)
    summary = intelligence.summary()

    assert summary["symbol_count"] > 0
    assert summary["dependency_count"] > 0
    assert summary["package_count"] == 1
    assert summary["source_file_count"] == 5
    assert summary["test_file_count"] == 1
    assert summary["project_types"] == ["python"]
    assert "python -m pytest" in summary["test_commands"]
    assert summary["cycles"] == []
