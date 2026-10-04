from forge.builder.universal import UniversalBuildEngine
from forge.testing.universal import UniversalTestEngine


def test_universal_build_detects_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    plan = UniversalBuildEngine(tmp_path).plan()
    assert plan is not None
    assert plan.system == "python"
    assert plan.evidence == ("pyproject.toml",)


def test_universal_build_never_executes_without_request(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    result = UniversalBuildEngine(tmp_path).build()
    assert result.planned
    assert not result.executed
    assert not result.passed
    assert "plan-only" in result.error


def test_universal_build_requires_authorization(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    result = UniversalBuildEngine(tmp_path).build(execute=True, approved=False)
    assert result.planned
    assert not result.executed
    assert "authorization" in result.error


def test_universal_test_detects_python_tests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    (tmp_path / "tests").mkdir()
    plan = UniversalTestEngine(tmp_path).discover()
    assert plan.primary is not None
    assert plan.primary.name == "pytest"


def test_universal_test_does_not_fake_missing_framework(tmp_path):
    plan = UniversalTestEngine(tmp_path).discover()
    assert plan.primary is None
