from forge.agents.coder import CoderAgent
from forge.agents.reviewer import ReviewerAgent
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
    (tmp_path / "tests" / "test_service.py").write_text(
        "from demo.service import create_user\n\n"
        "def test_create_user():\n"
        "    create_user()\n"
    )

    return RepositoryIntelligence.build(tmp_path)


def test_coder_keeps_existing_interface(tmp_path):
    intelligence = build_project(tmp_path)
    agent = CoderAgent()

    assert agent.name == "coder"
    assert "implementing" in agent.describe()

    context = agent.build_context(
        intelligence,
        "fix create_user",
        target_files=("demo/service.py",),
    )

    assert "demo/service.py" in context.files


def test_reviewer_keeps_existing_interface(tmp_path):
    intelligence = build_project(tmp_path)
    agent = ReviewerAgent()

    assert agent.name == "reviewer"
    assert "reviewing" in agent.describe()

    context = agent.build_context(
        intelligence,
        "review create_user",
        target_files=("demo/service.py",),
    )

    assert "demo/service.py" in context.files


def test_coder_respects_budget(tmp_path):
    intelligence = build_project(tmp_path)

    context = CoderAgent().build_context(
        intelligence,
        "fix create_user",
        target_files=("demo/service.py",),
        max_tokens=100,
    )

    assert context.estimated_tokens <= 100


def test_reviewer_context_is_deterministic(tmp_path):
    intelligence = build_project(tmp_path)
    agent = ReviewerAgent()

    first = agent.build_context(
        intelligence,
        "review create_user",
        target_files=("demo/service.py",),
    )

    second = agent.build_context(
        intelligence,
        "review create_user",
        target_files=("demo/service.py",),
    )

    assert first.fingerprint == second.fingerprint
