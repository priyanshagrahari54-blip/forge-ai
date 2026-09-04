from pathlib import Path

from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.repository import RepositoryIntelligence
from forge.intelligence.test_context import TestContextSelector


def make_project(root: Path) -> None:
    (root / "demo").mkdir()
    (root / "tests").mkdir()

    (root / "demo" / "__init__.py").write_text("")
    (root / "demo" / "models.py").write_text(
        "class User:\n"
        "    pass\n"
    )
    (root / "demo" / "service.py").write_text(
        "from demo.models import User\n\n"
        "def create_user():\n"
        "    return User()\n"
    )
    (root / "tests" / "test_service.py").write_text(
        "from demo.service import create_user\n\n"
        "def test_create_user():\n"
        "    create_user()\n"
    )


def test_selects_related_tests(tmp_path):
    make_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="service"),
        items=[ContextItem(path="demo/service.py", score=100)],
    )

    result = TestContextSelector(intelligence).select(pack)

    paths = {item.path for item in result.items}

    assert "demo/service.py" in paths
    assert "tests/test_service.py" in paths


def test_marks_selected_files_as_tests(tmp_path):
    make_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="service"),
        items=[ContextItem(path="demo/service.py")],
    )

    result = TestContextSelector(intelligence).select(pack)

    test_items = [
        item for item in result.items
        if item.path == "tests/test_service.py"
    ]

    assert len(test_items) == 1
    assert test_items[0].kind == "test"


def test_respects_include_tests_flag(tmp_path):
    make_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(
            task="service",
            include_tests=False,
        ),
        items=[ContextItem(path="demo/service.py")],
    )

    result = TestContextSelector(intelligence).select(pack)

    paths = {item.path for item in result.items}

    assert "tests/test_service.py" not in paths


def test_does_not_duplicate_existing_tests(tmp_path):
    make_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="service"),
        items=[
            ContextItem(path="demo/service.py"),
            ContextItem(
                path="tests/test_service.py",
                kind="test",
                score=90,
            ),
        ],
    )

    result = TestContextSelector(intelligence).select(pack)

    paths = [item.path for item in result.items]

    assert paths.count("tests/test_service.py") == 1
