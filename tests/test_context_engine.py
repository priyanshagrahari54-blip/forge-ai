from forge.intelligence.budget import ContextBudget, ContextBudgetManager
from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.context_pack import DeterministicContextPack
from forge.intelligence.context_query import ContextQueryEngine
from forge.intelligence.dependency_context import DependencyContextExpander
from forge.intelligence.repository import RepositoryIntelligence
from forge.intelligence.test_context import TestContextSelector


def build_project(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "demo" / "__init__.py").write_text("")
    (tmp_path / "demo" / "models.py").write_text(
        "class User:\n"
        "    pass\n"
    )
    (tmp_path / "demo" / "service.py").write_text(
        "from demo.models import User\n\n"
        "def create_user():\n"
        "    return User()\n"
    )
    (tmp_path / "demo" / "api.py").write_text(
        "from demo.service import create_user\n\n"
        "def endpoint():\n"
        "    return create_user()\n"
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "from demo.service import create_user\n\n"
        "def test_create_user():\n"
        "    create_user()\n"
    )

    return RepositoryIntelligence.build(tmp_path)


def test_context_pipeline_preserves_task_targets(tmp_path):
    intelligence = build_project(tmp_path)

    query = ContextQuery(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    pack = ContextQueryEngine(intelligence).query(query)

    assert "demo/service.py" in pack.files


def test_dependency_and_test_expansion_work_together(tmp_path):
    intelligence = build_project(tmp_path)

    query = ContextQuery(
        task="fix create_user",
        target_files=("demo/service.py",),
        include_tests=True,
        include_dependencies=True,
        include_dependents=True,
    )

    pack = ContextQueryEngine(intelligence).query(query)
    expanded = DependencyContextExpander(intelligence).expand(pack)
    selected = TestContextSelector(intelligence).select(expanded)

    paths = {item.path for item in selected.items}

    assert "demo/models.py" in paths
    assert "demo/api.py" in paths
    assert "tests/test_service.py" in paths


def test_budget_never_exceeds_limit(tmp_path):
    intelligence = build_project(tmp_path)

    query = ContextQuery(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    pack = ContextQueryEngine(intelligence).query(query)
    expanded = DependencyContextExpander(intelligence).expand(pack)
    selected = TestContextSelector(intelligence).select(expanded)

    manager = ContextBudgetManager(
        ContextBudget(max_tokens=150)
    )

    result = manager.apply(selected)

    assert result.estimated_tokens <= 150


def test_final_pack_is_deterministic(tmp_path):
    intelligence = build_project(tmp_path)

    query = ContextQuery(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    def build_final():
        pack = ContextQueryEngine(intelligence).query(query)
        pack = DependencyContextExpander(intelligence).expand(pack)
        pack = TestContextSelector(intelligence).select(pack)
        pack = ContextBudgetManager(
            ContextBudget(max_tokens=1000)
        ).apply(pack).pack
        return DeterministicContextPack.normalize(pack)

    first = build_final()
    second = build_final()

    assert DeterministicContextPack.serialize(first) == (
        DeterministicContextPack.serialize(second)
    )

    assert DeterministicContextPack.fingerprint(first).value == (
        DeterministicContextPack.fingerprint(second).value
    )


def test_empty_context_is_safe():
    query = ContextQuery(task="nothing")

    pack = ContextPack(query=query)

    result = DeterministicContextPack.normalize(pack)

    assert result.items == []
    assert DeterministicContextPack.fingerprint(result).value


def test_budget_zero_returns_empty_pack():
    pack = ContextPack(
        query=ContextQuery(task="test"),
        items=[
            ContextItem(
                path="demo.py",
                score=100,
            )
        ],
    )

    result = ContextBudgetManager(
        ContextBudget(max_tokens=0)
    ).apply(pack)

    assert result.pack.items == []
    assert result.estimated_tokens == 0
