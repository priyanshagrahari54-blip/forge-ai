from forge.intelligence.agent_context import AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence


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


def test_builder_returns_agent_context(tmp_path):
    intelligence = build_project(tmp_path)

    context = AgentContextBuilder(intelligence).build(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    assert context.pack is not None
    assert context.estimated_tokens > 0
    assert len(context.fingerprint) == 64


def test_builder_includes_relevant_files(tmp_path):
    intelligence = build_project(tmp_path)

    context = AgentContextBuilder(intelligence).build(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    files = set(context.files)

    assert "demo/service.py" in files
    assert "demo/models.py" in files
    assert "tests/test_service.py" in files


def test_builder_respects_token_budget(tmp_path):
    intelligence = build_project(tmp_path)

    context = AgentContextBuilder(
        intelligence,
        max_tokens=150,
    ).build(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    assert context.estimated_tokens <= 150


def test_builder_is_deterministic(tmp_path):
    intelligence = build_project(tmp_path)

    builder = AgentContextBuilder(intelligence)

    first = builder.build(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    second = builder.build(
        task="fix create_user",
        target_files=("demo/service.py",),
    )

    assert first.fingerprint == second.fingerprint


def test_builder_supports_empty_context(tmp_path):
    intelligence = build_project(tmp_path)

    context = AgentContextBuilder(intelligence).build(
        task="nothing",
    )

    assert context.pack is not None
    assert len(context.fingerprint) == 64
