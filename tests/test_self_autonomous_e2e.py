import json, subprocess
from forge.models.provider import MockProvider
from forge.models.router import ModelInfo, ModelRouter
from forge.self_development.analyzer import ForgeSelfAnalyzer
from forge.self_development.improvements import ImprovementGenerator
from forge.self_development.executor import SelfDevelopmentExecutor


def test_self_development_uses_model_and_safe_transaction(tmp_path):
    (tmp_path / "app.py").write_text("# TODO: remove placeholder\ndef value(): pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import value\ndef test_value(): assert value() == 42\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", "app.py", "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    findings = ForgeSelfAnalyzer(tmp_path).analyze()["findings"]
    candidate = ImprovementGenerator().generate(findings)[0]
    provider = MockProvider(json.dumps({
        "changes": {"app.py": "def value(): return 42\n"},
        "explanation": "replace the placeholder with the tested implementation",
    }))
    router = ModelRouter([ModelInfo("self-model", "coding", available=True, provider=provider, capabilities=("coding", "debugging"))])
    result = SelfDevelopmentExecutor(root=tmp_path, router=router).execute_candidate(candidate)

    assert result.accepted
    assert subprocess.run(["git", "show", "--format=", "--name-only", "HEAD"], cwd=tmp_path, text=True, capture_output=True).stdout.splitlines() == ["app.py"]
    assert router.history and router.history[0]["success"]
