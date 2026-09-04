from pathlib import Path

from forge.intelligence.context import ContextQuery
from forge.intelligence.context_query import ContextQueryEngine
from forge.intelligence.repository import RepositoryIntelligence


def create_project(root: Path) -> None:
    (root / "demo").mkdir()
    (root / "tests").mkdir()

    (root / "demo" / "__init__.py").write_text("")
    (root / "demo" / "models.py").write_text(
        "class User:\n"
        "    pass\n"
    )
    (root / "demo" / "service.py").write_text(
        "from demo.models import User\n\n"
        "def authenticate(user: User) -> bool:\n"
        "    return True\n"
    )
    (root / "demo" / "__main__.py").write_text(
        "from demo.service import authenticate\n"
    )
    (root / "tests" / "test_service.py").write_text(
        "from demo.service import authenticate\n\n"
        "def test_authenticate():\n"
        "    assert authenticate(None)\n"
    )


def test_context_query_selects_target_symbol(tmp_path: Path) -> None:
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)
    engine = ContextQueryEngine(intelligence)

    pack = engine.query(
        ContextQuery(
            task="Fix authenticate",
            target_symbols=("authenticate",),
        )
    )

    assert "demo/service.py" in pack.files


def test_context_query_expands_dependencies(tmp_path: Path) -> None:
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)
    engine = ContextQueryEngine(intelligence)

    pack = engine.query(
        ContextQuery(
            task="Fix authenticate",
            target_symbols=("authenticate",),
            include_dependencies=True,
        )
    )

    assert "demo/models.py" in pack.files


def test_context_query_includes_tests(tmp_path: Path) -> None:
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)
    engine = ContextQueryEngine(intelligence)

    pack = engine.query(
        ContextQuery(
            task="Fix authenticate",
            target_symbols=("authenticate",),
            include_tests=True,
        )
    )

    assert "tests/test_service.py" in pack.files


def test_context_query_respects_file_budget(tmp_path: Path) -> None:
    create_project(tmp_path)

    intelligence = RepositoryIntelligence.build(tmp_path)
    engine = ContextQueryEngine(intelligence)

    pack = engine.query(
        ContextQuery(
            task="Fix authenticate",
            target_symbols=("authenticate",),
            max_files=2,
        )
    )

    assert len(pack.files) <= 2
