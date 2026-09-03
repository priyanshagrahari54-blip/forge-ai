from pathlib import Path

from forge.intelligence.dependencies import DependencyGraph
from forge.intelligence.test_mapping import (
    RegressionSelector,
    TestMapper,
    TestMapping,
)


def create_project(root: Path) -> None:
    (root / ".gitignore").write_text(
        "__pycache__/\n",
        encoding="utf-8",
    )

    (root / "app.py").write_text(
        "from service import run\n\n"
        "def main():\n"
        "    run()\n",
        encoding="utf-8",
    )

    (root / "service.py").write_text(
        "from models import User\n\n"
        "def run():\n"
        "    return User()\n",
        encoding="utf-8",
    )

    (root / "models.py").write_text(
        "class User:\n"
        "    pass\n",
        encoding="utf-8",
    )

    tests = root / "tests"
    tests.mkdir()

    (tests / "test_app.py").write_text(
        "from app import main\n\n"
        "def test_main():\n"
        "    main()\n",
        encoding="utf-8",
    )

    (tests / "test_service.py").write_text(
        "from service import run\n\n"
        "def test_run():\n"
        "    run()\n",
        encoding="utf-8",
    )

    (tests / "test_models.py").write_text(
        "from models import User\n\n"
        "def test_user():\n"
        "    User()\n",
        encoding="utf-8",
    )

    (tests / "unrelated_test.py").write_text(
        "def test_other():\n"
        "    pass\n",
        encoding="utf-8",
    )


def test_direct_filename_mapping(tmp_path: Path):
    create_project(tmp_path)

    mapping = TestMapper(tmp_path).build()

    assert mapping.tests_for_source("app.py") == [
        "tests/test_app.py"
    ]

    assert mapping.tests_for_source("service.py") == [
        "tests/test_service.py"
    ]

    assert mapping.tests_for_source("models.py") == [
        "tests/test_models.py"
    ]


def test_reverse_mapping(tmp_path: Path):
    create_project(tmp_path)

    mapping = TestMapper(tmp_path).build()

    assert mapping.sources_for_test("tests/test_service.py") == [
        "service.py"
    ]


def test_dependency_aware_mapping(tmp_path: Path):
    create_project(tmp_path)

    graph = DependencyGraph()
    graph.add(
        "tests/test_app.py",
        "app.py",
        "internal",
        "app.py",
    )
    graph.add(
        "app.py",
        "service.py",
        "internal",
        "service.py",
    )
    graph.add(
        "service.py",
        "models.py",
        "internal",
        "models.py",
    )

    mapping = TestMapper(
        tmp_path,
        dependency_graph=graph,
    ).build()

    assert "tests/test_app.py" in mapping.tests_for_source("app.py")
    assert "tests/test_app.py" in mapping.tests_for_source("service.py")
    assert "tests/test_app.py" in mapping.tests_for_source("models.py")


def test_regression_selector():
    mapping = TestMapping()

    mapping.add("service.py", "tests/test_service.py")
    mapping.add("service.py", "tests/test_integration.py")
    mapping.add("service.py", "tests/test_service.py")

    selector = RegressionSelector(mapping)
    scope = selector.select("service.py")

    assert scope.changed_source == "service.py"
    assert scope.tests == [
        "tests/test_integration.py",
        "tests/test_service.py",
    ]


def test_unrelated_test_is_not_matched(tmp_path: Path):
    create_project(tmp_path)

    mapping = TestMapper(tmp_path).build()

    assert "tests/unrelated_test.py" not in (
        mapping.tests_for_source("app.py")
    )
