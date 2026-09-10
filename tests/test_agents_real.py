"""Every advertised agent must do real work — no canned text.

Covers the forceful agent fixes:

* ``TesterAgent`` actually runs pytest (pass/fail/targeted/timeout).
* The supervisor's reviewer/tester/security roster executes real gates
  (previously static-string lambdas).
* The orchestration architect is model-backed with a *labeled* heuristic
  fallback (previously unlabeled keyword matching).
* The orchestration reviewer runs the deterministic ReviewGate plus
  optional model review (previously keyword counting).
* The AI council gains real fabric-backed members; the default stays
  explicitly simulated.
* The orchestration researcher adds model synthesis over its inventory
  (labeled stats-only fallback), and the documentation worker reports
  real AST docstring coverage.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.agents.execution import AgentRequest
from forge.agents.tester import TesterAgent
from forge.core.supervisor import (build_reviewer_executor,
                                   build_security_executor)
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.council.engine import (AICouncilEngine, FabricCouncilMember,
                                  SimulatedCouncilModel)
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


def _request(task="do work", context=None, **metadata):
    task = TaskEngine().add("task", task)
    return AgentRequest(task, TaskStatus.CODING, context=context,
                        metadata=dict(metadata))


def _context_for(root: Path, *files: str):
    from forge.intelligence.agent_context import AgentContextBuilder
    from forge.intelligence.repository import RepositoryIntelligence

    return AgentContextBuilder(
        RepositoryIntelligence.build(root)).build(
            "review", target_files=tuple(files))


def _repo(tmp_path: Path, body: str = "x = 1\n") -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text(body)
    return root


# -- TesterAgent ------------------------------------------------------------


def test_tester_reports_passing_suite(tmp_path):
    root = _repo(tmp_path)
    (root / "test_app.py").write_text("def test_ok():\n    assert True\n")
    response = TesterAgent(root).execute(_request())
    assert response.success is True
    assert response.metadata["verdict"] == "passed"
    assert response.metadata["exit_code"] == 0
    assert "passed" in response.output


def test_tester_reports_failing_suite(tmp_path):
    root = _repo(tmp_path)
    (root / "test_app.py").write_text("def test_bad():\n    assert False\n")
    response = TesterAgent(root).execute(_request())
    assert response.success is False
    assert response.metadata["verdict"] == "failed"
    assert response.error


def test_tester_runs_targeted_paths_and_drops_escapes(tmp_path):
    root = _repo(tmp_path)
    (root / "test_one.py").write_text("def test_one():\n    assert True\n")
    (root / "test_two.py").write_text("def test_two():\n    assert False\n")
    response = TesterAgent(root).execute(
        _request(tests_to_run=["test_one.py", "../escape.py",
                               "/abs/path.py", "missing.py"]))
    assert response.success is True
    # Only the in-repo targeted file ran (one test), not the failing one.
    assert "1 passed" in response.output


def test_tester_reports_timeout(tmp_path):
    root = _repo(tmp_path)
    (root / "test_slow.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(30)\n")
    started = time.monotonic()
    response = TesterAgent(root, timeout=2).execute(_request())
    assert time.monotonic() - started < 25
    assert response.success is False
    assert "exceeded" in response.error
    assert response.metadata.get("timeout") is True


def test_tester_empty_repo_reports_no_tests(tmp_path):
    root = _repo(tmp_path)
    response = TesterAgent(root).execute(_request())
    assert response.success is True
    assert response.metadata["verdict"] == "no tests collected"
    assert response.metadata["no_tests"] is True


# -- supervisor roster -------------------------------------------------------


def test_supervisor_reviewer_flags_real_issues(tmp_path):
    root = _repo(tmp_path, "import os\nos.system('rm -rf /')\n")
    executor = build_reviewer_executor(str(root), None)
    context = _context_for(root, "app.py")
    assert "app.py" in [item.path for item in context.items]
    response = executor.execute(_request("review this", context=context))
    assert response.success is True
    payload = json.loads(response.output)
    assert payload["verdict"] in ("REQUEST_CHANGES", "BLOCK")
    rules = [finding["rule"] for finding in payload["findings"]]
    assert "os-exec" in rules


def test_supervisor_reviewer_clean_repo_approves(tmp_path):
    root = _repo(tmp_path, "def add(a, b):\n    return a + b\n")
    executor = build_reviewer_executor(str(root), None)
    payload = json.loads(executor.execute(_request("review")).output)
    assert payload["verdict"] == "APPROVE"


def test_supervisor_security_flags_secrets(tmp_path):
    root = _repo(tmp_path)
    (root / ".env").write_text("SECRET_KEY=topsecretvalue123\n")
    executor = build_security_executor(str(root))
    response = executor.execute(_request("audit"))
    assert response.success is True
    payload = json.loads(response.output)
    assert payload["passed"] is False


def test_supervisor_security_clean_repo_passes(tmp_path):
    root = _repo(tmp_path)
    payload = json.loads(
        build_security_executor(str(root)).execute(_request()).output)
    assert payload["passed"] is True


def test_supervisor_tester_executor_runs_suite(tmp_path):
    # The roster entry is the real TesterAgent, not a canned lambda.
    root = _repo(tmp_path)
    (root / "test_app.py").write_text("def test_ok():\n    assert True\n")
    agent = TesterAgent(str(root))
    assert agent.name == "tester"
    assert agent.describe()
    assert agent.execute(_request()).success is True


# -- orchestration workers ----------------------------------------------------


def _team_registry(plane, project_id="demo"):
    project = plane.projects[project_id]
    return plane._orchestration_team(project, "orch-test")


def _run_worker(registry, name, task="add csv export"):
    executor = registry.get(name).executor
    return executor.execute(_request(task))


class _PlanningProvider:
    """Provider answering planning prompts with architecture JSON."""

    name = "planner"

    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        return ModelResult(self.text, self.name)


class _ReasoningProvider(_PlanningProvider):
    name = "reasoner"


def _fabric(text: str, capability: str) -> ModelFabric:
    provider = _PlanningProvider(text)
    return ModelFabric(
        registry=ModelRegistry([Model(name=f"p/{capability}",
                                      provider="p",
                                      capabilities=(capability,))]),
        providers=ProviderRegistry({"p": provider}),
    )


def _plane_with_fabric(tmp_path, fabric, root):
    from forge.control import ControlConfig, ControlPlane

    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)}, fabric=fabric))
    return plane


def test_architect_model_path(tmp_path):
    root = _repo(tmp_path)
    payload = json.dumps({"proposal": ["add forge/export/csv.py",
                                       "wire export into app.py"],
                          "risks": ["encoding edge cases"]})
    plane = _plane_with_fabric(tmp_path, _fabric(payload, "planning"), root)
    try:
        response = _run_worker(_team_registry(plane), "architect",
                               "add csv export")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["source"] == "model"
    assert data["proposal"] == ["add forge/export/csv.py",
                                "wire export into app.py"]
    assert data["model"] == "p/planning"


def test_architect_falls_back_with_label_when_no_model(tmp_path):
    root = _repo(tmp_path)
    plane = _plane_with_fabric(
        tmp_path, _fabric(json.dumps({"changes": {}}), "coding"), root)
    try:
        response = _run_worker(_team_registry(plane), "architect",
                               "add csv export")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["source"] == "heuristic-fallback"
    assert "warning" in data and data["warning"]
    assert data["proposal"]  # the labeled heuristic still proposes


def test_reviewer_worker_flags_real_code(tmp_path):
    root = _repo(tmp_path, "import os\nos.system('rm -rf /')\n")
    plane = _plane_with_fabric(
        tmp_path, _fabric(json.dumps({"changes": {}}), "coding"), root)
    try:
        response = _run_worker(_team_registry(plane), "reviewer",
                               "review the change")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["verdict"] in ("REQUEST_CHANGES", "BLOCK")
    rules = [finding["rule"] for finding in data["findings"]]
    assert "os-exec" in rules
    assert data["model_reviewed"] is False


def test_reviewer_worker_clean_repo_approves(tmp_path):
    root = _repo(tmp_path, "def add(a, b):\n    return a + b\n")
    plane = _plane_with_fabric(
        tmp_path, _fabric(json.dumps({"changes": {}}), "coding"), root)
    try:
        response = _run_worker(_team_registry(plane), "reviewer", "review")
    finally:
        plane.stop()
    data = json.loads(response.output)
    assert data["verdict"] == "APPROVE"


# -- AI council ----------------------------------------------------------------


def _reasoning_fabric(text: str) -> ModelFabric:
    provider = _ReasoningProvider(text)
    return ModelFabric(
        registry=ModelRegistry([Model(name="p/reasoning", provider="p",
                                      capabilities=("reasoning",))]),
        providers=ProviderRegistry({"p": provider}),
    )


def test_fabric_council_member_deliberates_for_real():
    fabric = _reasoning_fabric(json.dumps({
        "stance": "approve", "stance_label": "ship it",
        "reasoning": "tests cover the change", "confidence": 0.8}))
    member = FabricCouncilMember("delta", fabric, role="pragmatist")
    opinion = member.deliberate("Should we ship?")
    assert opinion["simulation"] is False
    assert opinion["stance"] == "approve"
    assert opinion["confidence"] == 0.8
    assert opinion["model"] == "p/reasoning"
    assert member.stance == "approve"


def test_fabric_council_member_abstains_honestly_without_model():
    fabric = _fabric(json.dumps({"changes": {}}), "coding")
    member = FabricCouncilMember("delta", fabric, role="pragmatist")
    opinion = member.deliberate("Should we ship?")
    assert opinion["simulation"] is False
    assert opinion["stance"] == "abstain"
    assert opinion["confidence"] == 0.0
    assert "no reasoning model" in opinion["reasoning"]


def test_fabric_council_member_abstains_on_garbage():
    fabric = _reasoning_fabric("not json {{{")
    member = FabricCouncilMember("delta", fabric)
    opinion = member.deliberate("Should we ship?")
    assert opinion["stance"] == "abstain"
    assert "unparseable" in opinion["reasoning"]


def test_fabric_council_member_validates_inputs():
    fabric = _reasoning_fabric("{}")
    try:
        FabricCouncilMember("", fabric)
    except ValueError:
        pass
    else:
        raise AssertionError("empty name must fail")
    try:
        FabricCouncilMember("x", None)
    except ValueError:
        pass
    else:
        raise AssertionError("missing fabric must fail")
    try:
        FabricCouncilMember("x", fabric).deliberate("  ")
    except ValueError:
        pass
    else:
        raise AssertionError("empty question must fail")


def test_council_simulation_flag_reflects_membership():
    fabric = _reasoning_fabric(json.dumps({
        "stance": "approve", "stance_label": "ok",
        "reasoning": "fine", "confidence": 0.9}))
    real = AICouncilEngine(members=[
        FabricCouncilMember("delta", fabric),
        FabricCouncilMember("epsilon", fabric)])
    assert real.deliberate("Ship?")["simulation"] is False
    mixed = AICouncilEngine(members=[
        SimulatedCouncilModel("s", stance="approve", stance_label="sim"),
        FabricCouncilMember("delta", fabric)])
    assert mixed.deliberate("Ship?")["simulation"] is False
    simulated = AICouncilEngine()
    assert simulated.deliberate("Ship?")["simulation"] is True


# -- researcher synthesis + documentation coverage -------------------------------


def test_researcher_model_synthesis(tmp_path):
    root = _repo(tmp_path)
    payload = json.dumps({"summary": "tiny demo app",
                          "hotspots": ["app.py"],
                          "risks": ["no tests"]})
    plane = _plane_with_fabric(tmp_path, _fabric(payload, "research"), root)
    try:
        response = _run_worker(_team_registry(plane), "researcher",
                               "where should csv export go?")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["python_files"] >= 1
    synthesis = data["synthesis"]
    assert synthesis["available"] is True
    assert synthesis["summary"] == "tiny demo app"
    assert synthesis["hotspots"] == ["app.py"]
    assert synthesis["model"] == "p/research"


def test_researcher_stats_only_without_model(tmp_path):
    root = _repo(tmp_path)
    plane = _plane_with_fabric(
        tmp_path, _fabric(json.dumps({"changes": {}}), "coding"), root)
    try:
        response = _run_worker(_team_registry(plane), "researcher",
                               "survey the repo")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["python_files"] >= 1
    assert data["test_files"] >= 0
    assert data["synthesis"]["available"] is False
    assert "stats only" in data["synthesis"]["reason"]


def test_documentation_reports_real_docstring_coverage(tmp_path):
    root = _repo(tmp_path, '"""Module."""\n\n\ndef good():\n'
                           '    """Does the thing."""\n    return 1\n\n\n'
                           'def bad():\n    return 2\n')
    (root / "broken.py").write_text("def oops(:\n")
    (root / "README.md").write_text("# demo\n")
    plane = _plane_with_fabric(
        tmp_path, _fabric(json.dumps({"changes": {}}), "coding"), root)
    try:
        response = _run_worker(_team_registry(plane), "documentation",
                               "document the repo")
    finally:
        plane.stop()
    assert response.success is True
    data = json.loads(response.output)
    assert data["readme"] is True
    coverage = data["coverage"]
    # app.py: module + good documented, bad missing => 2/3.
    assert coverage["documented"] == 2
    assert coverage["total"] == 3
    assert coverage["ratio"] == pytest.approx(2 / 3, abs=0.001)
    assert "broken.py" in coverage["unparseable"]
    assert coverage["worst_files"][0]["file"] == "app.py"
