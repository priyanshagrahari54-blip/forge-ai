"""Permission decision semantics tests (A32 hardening).

Approval is evaluated at the permission boundary, not as a blanket
precondition: read-only work proceeds without write approval, while every
write and the commit itself must pass the PolicyGate. ALLOW / DENY /
REQUIRE_APPROVAL are never conflated, and DENY cannot be bypassed.
"""
import json
import subprocess

from forge.agents.coder import CoderAgent
from forge.agents.debugger import DebuggerAgent, TestDebugLoop
from forge.agents.execution import AgentRequest
from forge.core.supervisor import Supervisor
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.router import ModelInfo, ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _repo(root):
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


class ScriptedLoop:
    name = "scripted-loop"

    def __init__(self, coder_payload):
        self.coder_payload = coder_payload
        self.prompts = []

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        self.prompts.append(prompt)
        if "Review the following change set" in prompt:
            return ModelResult(
                json.dumps({"findings": [], "verdict": "APPROVE"}), self.name)
        return ModelResult(self.coder_payload, self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a32", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def _commits(root):
    return subprocess.run(["git", "log", "--oneline"], cwd=root, text=True,
                          capture_output=True).stdout


def _decisions(report, operation=None):
    events = [item for item in report["events"]
              if item["name"] == "permission_decision"]
    if operation is not None:
        events = [item for item in events
                  if item["details"]["operation"] == operation]
    return events


def _applier(root, mode):
    runtime = create_default_runtime(PermissionManager(mode=mode), str(root))
    return ChangeApplier(runtime, CheckpointManager(root), root=str(root))


CSV_PAYLOAD = json.dumps({
    "summary": "Add CSV export",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": "def health(): return True\n\ndef export_csv():\n    return 'x'\n"},
        {"path": "tests/test_csv.py", "action": "create",
         "content": "from app import export_csv\ndef test_csv():\n    assert export_csv() == 'x'\n"},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "added export",
    "risk_level": "low",
})


# -- Test A: read-only run needs no write approval --------------------------

def test_read_only_phases_run_without_write_approval(tmp_path):
    _repo(tmp_path)
    provider = ScriptedLoop(CSV_PAYLOAD)
    outcome = Supervisor("approval-a", tmp_path).run(
        "Add CSV export functionality and tests", approved=False,
        fabric=_fabric(provider))

    # Read-only phases completed: plan, agents, model generation.
    assert outcome["stages"] == ["PLAN", "AGENTS", "MODEL", "ROLLBACK"]
    assert outcome["plan"]["agents"]
    assert outcome["context_fingerprint"]
    assert provider.prompts  # the model was asked for a proposal
    assert outcome["mode"] == "assisted"
    assert outcome["report"]["mode"] == "assisted"

    # The run stopped exactly at the write boundary: nothing was modified.
    assert not outcome["accepted"]
    assert "not permitted" in outcome["error"]
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert not (tmp_path / "tests" / "test_csv.py").exists()
    assert _commits(tmp_path).count("initial") == 1

    decisions = _decisions(outcome["report"], "write_file")
    assert decisions
    assert all(item["details"]["decision"] == "REQUIRE_APPROVAL"
               for item in decisions)


def test_tests_run_without_write_approval(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    loop = TestDebugLoop(tmp_path, max_retries=0,
                         debugger=DebuggerAgent(str(tmp_path)))
    result = loop.run("verify", approved=False)
    assert result.success


def test_run_tests_tool_rejects_non_pytest_commands(tmp_path):
    runtime = create_default_runtime(PermissionManager(), str(tmp_path))
    refused = runtime.execute("run_tests", command=["rm", "-rf", "/"])
    assert not refused.success
    refused = runtime.execute(
        "run_tests", command=["/bin/pytest", "-q"])
    assert not refused.success
    refused = runtime.execute(
        "run_tests",
        command=["/usr/bin/python3", "-m", "pytest", "../outside.py"])
    assert not refused.success


def test_run_tests_blocked_in_safe_mode(tmp_path):
    import sys
    runtime = create_default_runtime(
        PermissionManager(mode=OperationMode.SAFE), str(tmp_path))
    refused = runtime.execute(
        "run_tests", approved=True,
        command=[sys.executable, "-m", "pytest", "-q"])
    assert not refused.success


# -- Test B: low-risk autonomous write --------------------------------------

def test_autonomous_low_risk_write_allowed(tmp_path):
    result = _applier(tmp_path, OperationMode.AUTONOMOUS).apply(
        [CodeChange(path="app.py", content="x = 1\n", risk="LOW")],
        approved=False, capability="coding")
    assert result.success
    assert result.decisions[0]["decision"] == "ALLOW"
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


# -- Test C: approval-required write waits for approval ---------------------

def test_approval_required_write_applies_only_after_approval(tmp_path):
    applier = _applier(tmp_path, OperationMode.ASSISTED)
    blocked = applier.apply(
        [CodeChange(path="app.py", content="x = 1\n")], approved=False)
    assert not blocked.success
    assert blocked.error_details[0].code == "APPROVAL_REQUIRED"
    assert not (tmp_path / "app.py").exists()

    granted = applier.apply(
        [CodeChange(path="app.py", content="x = 1\n")], approved=True)
    assert granted.success
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


# -- Tests D + F: denial is absolute ----------------------------------------

def test_denied_write_never_modifies_even_when_approved(tmp_path):
    _repo(tmp_path)
    provider = ScriptedLoop(CSV_PAYLOAD)
    outcome = Supervisor("approval-d", tmp_path).run(
        "Add CSV export", approved=True, fabric=_fabric(provider),
        mode=OperationMode.SAFE)

    assert not outcome["accepted"]
    assert outcome["stages"] == ["PLAN", "AGENTS", "MODEL", "ROLLBACK"]
    decisions = _decisions(outcome["report"], "write_file")
    assert decisions
    assert all(item["details"]["decision"] == "DENY" for item in decisions)
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert not (tmp_path / "tests" / "test_csv.py").exists()
    assert _commits(tmp_path).count("initial") == 1


def test_git_commit_denied_in_safe_mode_despite_approval():
    gate = PolicyGate(PermissionManager(mode=OperationMode.SAFE))
    outcome = gate.evaluate(operation="git_commit", tool="git", approved=True)
    assert outcome.decision == PolicyDecision.DENY
    assert not outcome.allowed


# -- Test E: high-risk writes -------------------------------------------------

def test_high_risk_write_requires_approval_then_applies(tmp_path):
    applier = _applier(tmp_path, OperationMode.AUTONOMOUS)
    blocked = applier.apply(
        [CodeChange(path="app.py", content="x = 1\n", risk="HIGH")],
        approved=False)
    assert not blocked.success
    assert blocked.decisions[0]["decision"] == "REQUIRE_APPROVAL"
    assert not (tmp_path / "app.py").exists()

    granted = applier.apply(
        [CodeChange(path="app.py", content="x = 1\n", risk="HIGH")],
        approved=True)
    assert granted.success
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


# -- commit is a permission boundary ------------------------------------------

def test_commit_blocked_without_approval_in_autonomous(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("approval-commit", tmp_path).run(
        "Add CSV export functionality and tests", approved=False,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)),
        mode=OperationMode.AUTONOMOUS)

    # Writes flowed (autonomous allows low-risk writes) but the commit did not.
    assert not outcome["accepted"]
    assert "ACCEPTANCE" in outcome["stages"]
    assert "CHECKPOINT" in outcome["stages"]
    assert "COMMIT" not in outcome["stages"]
    assert "commit not permitted" in outcome["error"]
    commit_decisions = _decisions(outcome["report"], "git_commit")
    assert len(commit_decisions) == 1
    assert commit_decisions[0]["details"]["decision"] == "REQUIRE_APPROVAL"
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"
    assert not (tmp_path / "tests" / "test_csv.py").exists()
    assert _commits(tmp_path).count("initial") == 1


def test_autonomous_run_commits_with_explicit_approval(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("approval-commit-ok", tmp_path).run(
        "Add CSV export functionality and tests", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)),
        mode=OperationMode.AUTONOMOUS)
    assert outcome["accepted"]
    assert outcome["report"]["mode"] == "autonomous"
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True).stdout.splitlines()
    assert committed == ["app.py", "tests/test_csv.py"]


# -- fabric is the canonical route ---------------------------------------------

def _request():
    task = TaskEngine().add("t", "add")
    return AgentRequest(task, TaskStatus.CODING, instructions="add",
                        metadata={"approved": True})


def test_fabric_route_is_marked_canonical(tmp_path):
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})

    class Scripted:
        name = "s"

        def generate(self, prompt, *, context="", task="", instructions="",
                     max_output_tokens=None, temperature=None):
            return ModelResult(payload, self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="s/m", provider="s", capabilities=("coding",))]),
        providers=ProviderRegistry({"s": Scripted()}))
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(_request())
    assert response.success
    assert response.metadata["routing"] == "fabric"


def test_legacy_router_route_is_marked_compatibility(tmp_path):
    from forge.models.provider import MockProvider
    payload = json.dumps({"changes": {"app.py": "x = 1\n"}})
    router = ModelRouter([ModelInfo("m", "coding", available=True,
                                    provider=MockProvider(payload))])
    response = CoderAgent(root=str(tmp_path), router=router).execute(_request())
    assert response.success
    assert response.metadata["routing"] == "legacy-router"
