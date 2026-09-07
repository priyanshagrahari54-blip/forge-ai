"""Test/debug/repair loop tests (A32.4 rebuild).

Repairs validate through the ChangeSet engine and authorize through the
policy gate; every failing execution yields a structured failure report and
every retry carries a recorded reason. Targeted runs execute only the
relevant tests while the acceptance gate still guards the full suite.
"""
import json
import sys

from forge.agents.debugger import DebuggerAgent, FailureReport, TestDebugLoop
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider, ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.router import ModelInfo, ModelRouter


def _router(payload):
    return ModelRouter([ModelInfo(
        "fixer", "debugging", available=True, provider=MockProvider(payload),
        capabilities=("debugging", "coding"))])


class _Scripted:
    name = "scripted"

    def __init__(self, response):
        self.response = response

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        return ModelResult(self.response, self.name)


def _fabric(payload):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="s/m", provider="s", capabilities=("debugging", "coding"))]),
        providers=ProviderRegistry({"s": _Scripted(payload)}),
    )


def _failing_repo(root):
    (root / "app.py").write_text("def value(): return 1\n")
    (root / "test_app.py").write_text(
        "import app\ndef test_value():\n    assert app.value() == 2\n")


# -- repairs validate through the ChangeSet engine ------------------------

def test_repair_rejects_secret_content_without_partial_write(tmp_path):
    _failing_repo(tmp_path)
    payload = json.dumps({"changes": {
        "app.py": "api_key = 'sup3r-secret-value'\ndef value(): return 2\n"},
        "explanation": "fix with a secret"})
    loop = TestDebugLoop(tmp_path, max_retries=1,
                         debugger=DebuggerAgent(str(tmp_path), router=_router(payload)))
    result = loop.run("make value 2", approved=True)
    assert not result.success
    assert result.final_state == "repair failed"
    assert result.attempts[0].modifications == {}
    assert "repair failed" in result.attempts[0].reason
    assert result.failures[0].exit_code == 1
    # The rejected repair left no trace.
    assert (tmp_path / "app.py").read_text() == "def value(): return 1\n"


def test_repair_rejects_traversal_path_via_fabric(tmp_path):
    _failing_repo(tmp_path)
    payload = json.dumps({"changes": {"../a32_evil.py": "x = 1\n"},
                          "explanation": "escape"})
    debugger = DebuggerAgent(str(tmp_path), fabric=_fabric(payload))
    result = TestDebugLoop(tmp_path, max_retries=1, debugger=debugger).run(
        "make value 2", approved=True)
    assert not result.success
    assert result.final_state == "repair failed"
    assert not (tmp_path.parent / "a32_evil.py").exists()
    assert (tmp_path / "app.py").read_text() == "def value(): return 1\n"


def test_repair_rejects_invalid_python(tmp_path):
    _failing_repo(tmp_path)
    payload = json.dumps({"changes": {"app.py": "def broken(:\n"},
                          "explanation": "broken"})
    loop = TestDebugLoop(tmp_path, max_retries=1,
                         debugger=DebuggerAgent(str(tmp_path), router=_router(payload)))
    result = loop.run("make value 2", approved=True)
    assert not result.success
    assert (tmp_path / "app.py").read_text() == "def value(): return 1\n"


# -- structured failure reports -------------------------------------------

def test_failures_carry_command_exit_code_output_and_reason(tmp_path):
    _failing_repo(tmp_path)
    payload = json.dumps({"changes": {"app.py": "def value(): return 1\n"},
                          "explanation": "wrong fix"})
    loop = TestDebugLoop(tmp_path, max_retries=2,
                         debugger=DebuggerAgent(str(tmp_path), router=_router(payload)))
    result = loop.run("make value 2", approved=True)
    assert not result.success
    assert len(result.attempts) == 2
    for attempt in result.attempts:
        assert "bounded repair" in attempt.reason
        assert attempt.failure is not None
        assert attempt.command and attempt.command[0] == sys.executable
        assert attempt.exit_code == 1
    # Two repaired failures plus the final bound-reached failure.
    assert len(result.failures) == 3
    assert "retry bound" in result.failures[-1].reason
    report = result.failures[0].to_dict()
    assert report["exit_code"] == 1
    assert report["output"] and report["diagnosis"] and report["reason"]
    assert isinstance(report["command"], list)


def test_no_fake_pass_with_zero_retries(tmp_path):
    _failing_repo(tmp_path)
    loop = TestDebugLoop(tmp_path, max_retries=0,
                         debugger=DebuggerAgent(str(tmp_path), router=_router("{}")))
    result = loop.run("make value 2", approved=True)
    assert not result.success
    assert result.error
    assert len(result.failures) == 1


def test_failure_report_defaults():
    report = FailureReport(attempt_number=1, command=["pytest"], exit_code=1,
                           output="FAILED", diagnosis="failed", reason="retry")
    assert report.to_dict()["command"] == ["pytest"]


# -- targeted runs ----------------------------------------------------------

def _split_repo(root):
    (root / "tests").mkdir()
    (root / "tests" / "test_target.py").write_text("def test_target():\n    assert True\n")
    (root / "tests" / "test_other.py").write_text("def test_other():\n    assert False\n")


def test_targeted_run_executes_only_relevant_tests(tmp_path):
    _split_repo(tmp_path)
    loop = TestDebugLoop(tmp_path, max_retries=0,
                         debugger=DebuggerAgent(str(tmp_path), router=_router("{}")))
    result = loop.run("targeted", approved=True, test_paths=["tests/test_target.py"])
    assert result.success
    assert result.attempts == []


def test_targeted_failure_records_scoped_command(tmp_path):
    _split_repo(tmp_path)
    loop = TestDebugLoop(tmp_path, max_retries=0,
                         debugger=DebuggerAgent(str(tmp_path), router=_router("{}")))
    result = loop.run("targeted", approved=True, test_paths=["tests/test_other.py"])
    assert not result.success
    assert result.failures[0].command[-1] == "tests/test_other.py"


def test_unsafe_test_paths_are_dropped(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_target.py").write_text("def test_target():\n    assert True\n")
    loop = TestDebugLoop(tmp_path, max_retries=0,
                         debugger=DebuggerAgent(str(tmp_path), router=_router("{}")))
    result = loop.run("targeted", approved=True,
                      test_paths=["../evil.py", "tests/test_target.py"])
    assert result.success
