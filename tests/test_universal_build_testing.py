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


def test_universal_test_detects_go_project_without_tests_directory(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/app\n\ngo 1.20\n")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    plan = UniversalTestEngine(tmp_path).discover()
    strategy = next(item for item in plan.strategies if item.name == "go-test")
    assert strategy.command == ("go", "test", "./...")
    assert "go.mod" in strategy.evidence

def test_universal_test_detects_dotnet_solution(tmp_path, monkeypatch):
    (tmp_path / "App.sln").write_text("")
    monkeypatch.setattr("forge.testing.universal.shutil.which", lambda name: "/usr/bin/dotnet" if name == "dotnet" else None)
    plan = UniversalTestEngine(tmp_path).discover()
    assert plan.primary.name == "dotnet-test"
    assert plan.primary.tool_available

def test_gradle_wrapper_is_recognized_without_being_on_path(tmp_path, monkeypatch):
    (tmp_path / "build.gradle.kts").write_text("plugins {}")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    monkeypatch.setattr("forge.testing.universal.shutil.which", lambda name: None)
    plan = UniversalTestEngine(tmp_path).discover()
    strategy = next(item for item in plan.strategies if item.name == "gradle-test")
    assert strategy.tool_available
    assert strategy.command == ("gradlew", "test")

def test_universal_test_detects_dart_tests(tmp_path, monkeypatch):
    (tmp_path / "pubspec.yaml").write_text("name: sample\n")
    (tmp_path / "test").mkdir()
    monkeypatch.setattr("forge.testing.universal.shutil.which", lambda name: "/usr/bin/dart" if name == "dart" else None)
    plan = UniversalTestEngine(tmp_path).discover()
    assert any(item.name == "dart-test" for item in plan.strategies)
