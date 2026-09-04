from pathlib import Path

from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.dependency_context import DependencyContextExpander
from forge.intelligence.repository import RepositoryIntelligence


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
    (root / "demo" / "api.py").write_text(
        "from demo.service import create_user\n\n"
        "def endpoint():\n"
        "    return create_user()\n"
    )


def test_expands_direct_dependencies(tmp_path):
    make_project(tmp_path)
    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="service"),
        items=[ContextItem(path="demo/service.py", score=100)],
    )

    result = DependencyContextExpander(intelligence).expand(
        pack, max_depth=1
    )

    paths = {item.path for item in result.items}

    assert "demo/service.py" in paths
    assert "demo/models.py" in paths


def test_expands_direct_dependents(tmp_path):
    make_project(tmp_path)
    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="models"),
        items=[ContextItem(path="demo/models.py", score=100)],
    )

    result = DependencyContextExpander(intelligence).expand(
        pack, max_depth=1
    )

    paths = {item.path for item in result.items}

    assert "demo/models.py" in paths
    assert "demo/service.py" in paths


def test_respects_depth(tmp_path):
    make_project(tmp_path)
    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="models"),
        items=[ContextItem(path="demo/models.py", score=100)],
    )

    result = DependencyContextExpander(intelligence).expand(
        pack, max_depth=2
    )

    paths = {item.path for item in result.items}

    assert "demo/service.py" in paths
    assert "demo/api.py" in paths


def test_does_not_duplicate_existing_items(tmp_path):
    make_project(tmp_path)
    intelligence = RepositoryIntelligence.build(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="service"),
        items=[
            ContextItem(path="demo/service.py", score=100),
            ContextItem(path="demo/models.py", score=90),
        ],
    )

    result = DependencyContextExpander(intelligence).expand(pack)

    paths = [item.path for item in result.items]

    assert paths.count("demo/service.py") == 1
    assert paths.count("demo/models.py") == 1
