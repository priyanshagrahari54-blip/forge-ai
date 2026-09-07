"""A33 platform integration with the A32 loop (tighten-only, no bypass)."""
import json
import subprocess

from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.supervisor import Supervisor
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.runtime.defaults import create_default_runtime
from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.audit import AuditLog
from forge.security.classification import ModelDataPolicy
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy import (
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _rule(rule_id, resource, operation, effect, scope="", **kwargs):
    return PermissionRule(rule_id, resource, operation, effect, scope,
                          reason="integration", **kwargs)


def _manager(mode=OperationMode.ASSISTED, policy=None, store=None, audit=None):
    return PermissionManager(mode=mode, policy=policy, store=store,
                             agent="test", audit=audit)


# -- runtime layer ------------------------------------------------------------

def test_engine_deny_tightens_runtime_writes(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("no-secrets", Resource.FILESYSTEM, "write", "DENY",
              "secrets/**")])
    runtime = create_default_runtime(_manager(policy=policy), str(tmp_path))
    denied = runtime.execute("write_file", approved=True, path="secrets/k",
                             content="x")
    assert not denied.success and "no-secrets" in denied.error
    assert not (tmp_path / "secrets" / "k").exists()
    allowed = runtime.execute("write_file", approved=True, path="src/a.py",
                              content="x = 1\n")
    assert allowed.success


def test_engine_allow_cannot_loosen_safe_mode(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("open", Resource.FILESYSTEM, "write", "ALLOW", "**")])
    runtime = create_default_runtime(
        _manager(OperationMode.SAFE, policy), str(tmp_path))
    denied = runtime.execute("write_file", approved=True, path="a.py",
                             content="x")
    assert not denied.success
    assert not (tmp_path / "a.py").exists()


def test_engine_approval_rule_tightens_autonomous_auto_allow(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("gate", Resource.FILESYSTEM, "write", "REQUIRE_APPROVAL",
              "src/**")])
    runtime = create_default_runtime(
        _manager(OperationMode.AUTONOMOUS, policy), str(tmp_path))
    # A32 would auto-allow; the explicit engine rule requires approval.
    blocked = runtime.execute("write_file", approved=False, path="src/a.py",
                              content="x")
    assert not blocked.success and "gate" in blocked.error
    assert not (tmp_path / "src" / "a.py").exists()
    granted = runtime.execute("write_file", approved=True, path="src/a.py",
                              content="x")
    assert granted.success


def test_token_cannot_override_mode_denial(tmp_path):
    store = ApprovalStore()
    manager = _manager(OperationMode.SAFE, store=store)
    runtime = create_default_runtime(manager, str(tmp_path))
    request = store.submit(ApprovalRequest(
        agent="test", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py",), reason="x"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")
    denied = runtime.execute("write_file", approved=False, path="a.py",
                             content="x", approval_token_id=token.id)
    assert not denied.success
    assert "SAFE mode" in denied.error
    assert not (tmp_path / "a.py").exists()


def test_engine_abstains_for_untranslatable_tools(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("deny-all", Resource.FILESYSTEM, "write", "DENY", "**")])
    runtime = create_default_runtime(_manager(policy=policy), str(tmp_path))
    # The search tool's permission key has no engine mapping: A32 decides.
    result = runtime.execute("search", approved=True, query="x")
    assert result.success


# -- gate + applier layer -------------------------------------------------------

def _applier_env(tmp_path, policy=None):
    store = ApprovalStore()
    manager = _manager(policy=policy, store=store)
    runtime = create_default_runtime(manager, str(tmp_path))
    applier = ChangeApplier(runtime, CheckpointManager(tmp_path),
                            root=str(tmp_path), approval_store=store)
    return applier, store


def test_token_satisfies_gate_without_blanket_approval(tmp_path):
    applier, store = _applier_env(tmp_path)
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("src/app.py",), task_id="t1", reason="feature"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")
    result = applier.apply(
        [CodeChange(path="src/app.py", content="x = 1\n")],
        approved=False, actor="coder", task_id="t1",
        approval_token_id=token.id)
    assert result.success
    assert "approval token" in result.decisions[0]["reason"].lower()
    assert (tmp_path / "src" / "app.py").read_text() == "x = 1\n"


def test_dry_run_with_token_consumes_nothing(tmp_path):
    applier, store = _applier_env(tmp_path)
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py",), task_id="t", reason="x"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")
    preview = applier.dry_run(
        [CodeChange(path="a.py", content="x = 1\n")],
        approved=False, actor="coder", task_id="t",
        approval_token_id=token.id)
    assert preview.valid
    assert preview.decisions[0]["decision"] == "ALLOW"
    # Preview consumed nothing: the token still authorizes the real apply.
    result = applier.apply(
        [CodeChange(path="a.py", content="x = 1\n")],
        approved=False, actor="coder", task_id="t",
        approval_token_id=token.id)
    assert result.success


def test_task_grant_issued_and_revoked_with_outcome(tmp_path):
    applier, store = _applier_env(tmp_path)
    good = applier.apply(
        [CodeChange(path="a.py", content="x = 1\n")],
        approved=True, actor="coder", task_id="t1")
    assert good.success
    assert good.task_grant is not None
    assert good.task_grant["files"] == ["a.py"]
    assert good.task_grant["task_id"] == "t1"
    assert len(store.active_grants("t1")) == 1

    bad = applier.apply(
        [CodeChange(path="b.py", content="ok = 1\n"),
         CodeChange(path="c.py", content="api_key = 'abcdef1234567890'")],
        approved=True, actor="coder", task_id="t2")
    assert not bad.success
    assert bad.task_grant is None
    assert store.active_grants("t2") == []
    assert not (tmp_path / "b.py").exists()  # rolled back


def test_applier_rollback_revokes_grant(tmp_path):
    applier, store = _applier_env(tmp_path)
    result = applier.apply([CodeChange(path="a.py", content="x = 1\n")],
                           approved=True, task_id="t9")
    assert result.success and len(store.active_grants("t9")) == 1
    applier.rollback(result)
    assert store.active_grants("t9") == []
    assert result.task_grant is None
    assert not (tmp_path / "a.py").exists()


def test_commit_gate_redeems_token_directly():
    store = ApprovalStore()
    manager = _manager(store=store)
    request = store.submit(ApprovalRequest(
        agent="supervisor", resource=Resource.GIT, operation="commit",
        task_id="t", reason="release"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")
    outcome = PolicyGate(manager).evaluate(
        operation="git_commit", tool="git", approved=False,
        agent="supervisor", task_id="t", approval_token_id=token.id)
    assert outcome.decision == PolicyDecision.ALLOW
    assert "token" in outcome.reason.lower()


def test_gate_and_runtime_audits_share_task_linkage(tmp_path):
    audit = AuditLog()
    applier, _ = _applier_env(tmp_path)
    applier.runtime.permission_manager.audit = audit
    applier.apply([CodeChange(path="a.py", content="x = 1\n")],
                  approved=True, actor="coder", task_id="t-link")
    events = audit.query(task_id="t-link")
    assert len(events) >= 2  # gate evaluation + tool execution
    assert all(event.decision == PolicyDecision.ALLOW for event in events)


# -- supervisor layer -------------------------------------------------------------

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

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
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

A32_EVENT_NAMES = frozenset({
    "task_started", "agents_selected", "model_selected", "change_proposed",
    "permission_decision", "change_applied", "test_executed", "test_failed",
    "repair_attempted", "review_result", "security_result",
    "benchmark_result", "acceptance_result", "commit", "rollback",
})


def test_supervisor_platform_run_grants_tasks_and_audits(tmp_path):
    _repo(tmp_path)
    store = ApprovalStore()
    audit = AuditLog()
    outcome = Supervisor("platform", tmp_path).run(
        "Add CSV export functionality and tests", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)),
        approval_store=store, audit_log=audit)

    assert outcome["accepted"] is True
    assert outcome["task_grant"]["files"] == ["app.py", "tests/test_csv.py"]
    assert outcome["task_grant"]["task_id"] == outcome["task"]["id"]
    # Temporary authority ended with the run.
    assert store.active_grants(outcome["task"]["id"]) == []
    # Every permission decision observable, linked to the task.
    assert outcome["audit_events"]
    assert all(item["task_id"] == outcome["task"]["id"]
               for item in outcome["audit_events"])
    assert any(item["decision"] == "ALLOW" for item in outcome["audit_events"])
    # Report events unchanged: no new event names leaked into A32 telemetry.
    names = {item["name"] for item in outcome["report"]["events"]}
    assert names <= A32_EVENT_NAMES


def test_supervisor_without_platform_has_no_platform_keys(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("plain", tmp_path).run(
        "Add CSV export functionality and tests", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)))
    assert outcome["accepted"] is True
    assert "task_grant" not in outcome
    assert "audit_events" not in outcome


def test_supervisor_policy_deny_blocks_writes(tmp_path):
    _repo(tmp_path)
    policy = PermissionPolicy(rules=[
        _rule("freeze", Resource.FILESYSTEM, "write", "DENY", "**")])
    outcome = Supervisor("frozen", tmp_path).run(
        "Add CSV export", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)), policy=policy)
    assert not outcome["accepted"]
    assert outcome["stages"] == ["PLAN", "AGENTS", "MODEL", "ROLLBACK"]
    assert "freeze" in outcome["error"]
    assert (tmp_path / "app.py").read_text() == "def health(): return True\n"


# -- model data policy --------------------------------------------------------------

class RecordingProvider:
    name = "recording"

    def __init__(self):
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append(prompt)
        return ModelResult('{"changes": {}}', self.name)


def _data_fabric(provider_name, local, policy=None):
    provider = RecordingProvider()
    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name=f"{provider_name}/m", provider=provider_name,
                  capabilities=("coding",), local=local),
        ]),
        providers=ProviderRegistry({provider_name: provider}),
        model_policy=policy)
    return fabric, provider


SECRET_PROMPT = "Rotate this: api_key = 'abcdef1234567890'"


def test_remote_model_blocked_for_secret_data():
    fabric, provider = _data_fabric("remote", False, ModelDataPolicy())
    response = fabric.generate(ModelRequest(prompt=SECRET_PROMPT,
                                            capability="coding"))
    assert not response.success
    assert "not authorized for secret data" in response.error
    assert provider.calls == []  # never sent


def test_local_model_permitted_for_secret_data():
    fabric, provider = _data_fabric("local", True, ModelDataPolicy())
    response = fabric.generate(ModelRequest(prompt=SECRET_PROMPT,
                                            capability="coding"))
    assert response.success
    assert provider.calls != []
    assert response.metadata["classification"] == "secret"


def test_explicit_authorization_permits_remote_secret():
    fabric, provider = _data_fabric("remote", False, ModelDataPolicy())
    request = ModelRequest(prompt=SECRET_PROMPT, capability="coding",
                           metadata={"data_authorized": True})
    assert fabric.generate(request).success
    assert provider.calls != []


def test_request_level_policy_overrides_fabric_default():
    fabric, provider = _data_fabric("remote", False)
    assert fabric.generate(SECRET_PROMPT).success  # no policy: legacy flow
    request = ModelRequest(prompt=SECRET_PROMPT, capability="coding",
                           metadata={"model_data_policy": ModelDataPolicy()})
    response = fabric.generate(request)
    assert not response.success
    assert len(provider.calls) == 1


def test_stream_respects_data_policy():
    fabric, provider = _data_fabric("remote", False, ModelDataPolicy())
    try:
        list(fabric.stream(ModelRequest(prompt=SECRET_PROMPT,
                                        capability="coding")))
    except Exception as exc:
        assert "not authorized for secret data" in str(exc)
    else:
        raise AssertionError("stream should have refused secret data")
    assert provider.calls == []


def test_coder_reports_context_classification(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    payload = json.dumps({"changes": {"app.py": "x = 2\n"}})

    class Scripted:
        name = "s"

        def generate(self, prompt, *, context="", task="", instructions="",
                     max_output_tokens=None, temperature=None):
            return ModelResult(payload, self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="s/m", provider="s", capabilities=("coding",))]),
        providers=ProviderRegistry({"s": Scripted()}))
    task = TaskEngine().add("t", "bump")
    request = AgentRequest(task, TaskStatus.CODING, instructions="bump",
                           metadata={"approved": True})
    response = CoderAgent(root=str(tmp_path), fabric=fabric).execute(request)
    assert response.success
    assert response.metadata["classification"] == "internal"
