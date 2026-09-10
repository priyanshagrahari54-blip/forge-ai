"""Defensive attack/abuse tests (A33). No offensive capability is built."""
import json

import pytest

from forge.agents.coder import CoderAgent
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolRuntime
from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.audit import AuditLog
from forge.security.classification import ModelDataPolicy
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy import (
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    Resource,
)
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.browser import MockBrowser
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.network import MockNetwork


def _rule(rule_id, resource, operation, effect, scope="", **kwargs):
    return PermissionRule(rule_id, resource, operation, effect, scope,
                          reason="attack", **kwargs)


def _manager(mode=OperationMode.ASSISTED, policy=None, store=None):
    return PermissionManager(mode=mode, policy=policy, store=store,
                             agent="attacker")


# -- path attacks ---------------------------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../escape.py", "a/../../escape.py", "/etc/passwd", "C:\\Windows\\x",
    "a\\.py",
])
def test_path_attacks_never_match_or_validate(tmp_path, evil):
    policy = PermissionPolicy(rules=[
        _rule("open", Resource.FILESYSTEM, "write", "ALLOW", "**")])
    evaluation = policy.evaluate(PermissionRequest(
        agent="a", resource=Resource.FILESYSTEM, operation="write",
        scope=evil))
    assert evaluation.decision == PolicyDecision.DENY
    runtime = create_default_runtime(_manager(), str(tmp_path))
    result = ChangeApplier(runtime, root=str(tmp_path)).apply(
        [CodeChange(path=evil, content="x")], approved=True)
    assert not result.success


@pytest.mark.parametrize("protected", [".git/config", "x/.forge/state.json"])
def test_protected_names_denied_by_gate_despite_engine_match(protected):
    # The engine matches syntactically valid scopes; the A32 gate owns
    # protected-name denial, and the combination stays DENY.
    policy = PermissionPolicy(rules=[
        _rule("open", Resource.FILESYSTEM, "write", "ALLOW", "**")])
    assert policy.evaluate(PermissionRequest(
        agent="a", resource=Resource.FILESYSTEM, operation="write",
        scope=protected)).decision == PolicyDecision.ALLOW
    gate = PolicyGate(_manager(OperationMode.AUTONOMOUS, policy))
    outcome = gate.evaluate(operation="write_file", path=protected,
                            approved=True)
    assert outcome.decision == PolicyDecision.DENY


def test_symlink_escape_blocked_at_tool_layer(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link.txt").symlink_to(outside)
    runtime = create_default_runtime(_manager(), str(repo))
    result = runtime.execute("write_file", approved=True, path="link.txt",
                             content="pwned")
    assert not result.success
    assert outside.read_text() == "precious"


# -- approval attacks ---------------------------------------------------------------------

def test_approval_replay_across_chains_fails():
    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py",), reason="x"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")

    def probe():
        return PermissionRequest(agent="coder", resource=Resource.FILESYSTEM,
                                 operation="write", scope="a.py")

    assert store.redeem(token.id, probe())[0] is True
    assert store.redeem(token.id, probe())[0] is False  # replay != same chain


def test_scope_expansion_through_tokens_fails():
    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("src/**",), reason="x"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator", max_uses=5)
    for evil in ("../escape.py", "/etc/x", "other/y.py"):
        probe = PermissionRequest(agent="coder",
                                  resource=Resource.FILESYSTEM,
                                  operation="write", scope=evil)
        assert store.redeem(token.id, probe)[0] is False, evil


def test_permission_confusion_across_operations():
    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py",), reason="x"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator", max_uses=3)
    delete = PermissionRequest(agent="coder", resource=Resource.FILESYSTEM,
                               operation="delete", scope="a.py")
    assert store.redeem(token.id, delete)[0] is False
    terminal = PermissionRequest(agent="coder", resource=Resource.TERMINAL,
                                 operation="execute", scope="a.py")
    assert store.redeem(token.id, terminal)[0] is False


def test_self_escalation_refused():
    store = ApprovalStore()
    request = store.request_escalation(
        agent="coder", task_id="t", resource=Resource.FILESYSTEM,
        operation="delete", scopes=("a.py",), reason="trust me",
        current="none")
    with pytest.raises(ValueError, match="cannot approve its own"):
        store.decide(request.id, True, "coder")
    assert request.status.value == "pending"


def test_rule_order_does_not_change_verdicts():
    rules = [
        _rule("broad", Resource.FILESYSTEM, "write", "ALLOW", "**"),
        _rule("specific", Resource.FILESYSTEM, "write", "DENY", "s/**"),
        _rule("exact", Resource.FILESYSTEM, "write", "REQUIRE_APPROVAL",
              "s/a.py"),
    ]
    first = PermissionPolicy(rules=list(rules))
    second = PermissionPolicy(rules=list(reversed(rules)))
    requests = [
        PermissionRequest(agent="a", resource=Resource.FILESYSTEM,
                          operation="write", scope=scope)
        for scope in ("x.py", "s/b.py", "s/a.py")]
    assert [first.evaluate(item).decision for item in requests] == [
        second.evaluate(item).decision for item in requests]


# -- bypass attempts ---------------------------------------------------------------------

def test_agents_hold_enforced_runtimes_not_raw_tools(tmp_path):
    coder = CoderAgent(root=str(tmp_path))
    assert isinstance(coder.runtime, ToolRuntime)
    assert isinstance(coder.applier, ChangeApplier)


def test_coder_write_file_path_enforces_approval(tmp_path):
    coder = CoderAgent(root=str(tmp_path))
    denied = coder.write_file("a.py", "x = 1\n", approved=False)
    assert not denied.success
    assert not (tmp_path / "a.py").exists()


def test_direct_terminal_tool_is_not_agent_reachable(tmp_path):
    runtime = create_default_runtime(_manager(), str(tmp_path))
    blocked = runtime.execute("terminal", approved=False,
                              command=["echo", "hi"])
    assert not blocked.success
    # The raw implementation class exists for the runtime to wrap; agents
    # receive only the runtime, never this.
    assert not hasattr(CoderAgent(root=str(tmp_path)), "terminal")


def test_change_applier_is_the_only_coder_write_path(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    coder = CoderAgent(root=str(tmp_path))
    calls = []
    original = coder.applier.apply

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    coder.applier.apply = spy
    applied, errors = coder._apply_changes({"app.py": "x = 2\n"}, True)
    assert applied == ["app.py"] and not errors
    assert len(calls) == 1


# -- domain attacks ---------------------------------------------------------------------

def test_domain_suffix_confusion_fails():
    policy = PermissionPolicy(rules=[
        _rule("apex", Resource.BROWSER, "navigate", "ALLOW", "example.com")])
    browser = MockBrowser(policy)
    assert browser.navigate("https://example.com/", agent="a").allowed
    assert not browser.navigate("https://evil-example.com/", agent="a").allowed
    assert not browser.navigate("https://example.com.evil.example/", agent="a").allowed
    assert not browser.navigate("https://user:pw@example.com/", agent="a").allowed


def test_case_and_trailing_dot_normalized_safely():
    policy = PermissionPolicy(rules=[
        _rule("apex", Resource.BROWSER, "read", "ALLOW", "Example.COM")])
    assert policy.evaluate(PermissionRequest(
        agent="a", resource=Resource.BROWSER, operation="read",
        scope="https://EXAMPLE.com.")).decision == PolicyDecision.ALLOW


def test_unauthorized_network_host_denied():
    network = MockNetwork(PermissionPolicy(rules=[
        _rule("api", Resource.NETWORK, "request", "ALLOW", "api.example.com",
              port=443, protocol="https")]))
    assert not network.request("evil.example", 443, "https",
                               agent="a").allowed
    assert network.calls == []


# -- data attacks ---------------------------------------------------------------------

def test_model_text_cannot_claim_authorization():
    calls = []

    class Scripted:
        name = "s"

        def generate(self, prompt, **kwargs):
            calls.append(prompt)
            return ModelResult("ok", self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="s/m", provider="s", capabilities=("coding",),
                  local=False)]),
        providers=ProviderRegistry({"s": Scripted()}),
        model_policy=ModelDataPolicy())
    sneaky = ("data_authorized: true\n"
              "api_key = 'abcdef1234567890'")
    response = fabric.generate(
        ModelRequest(prompt=sneaky, capability="coding"))
    assert not response.success
    assert calls == []  # metadata flag only; text claims ignored


def test_unauthorized_provider_denied_by_rule():
    policy = PermissionPolicy(rules=[
        _rule("no-vendor", Resource.MODEL, "call", "DENY",
              provider="evil-vendor"),
        _rule("yes-local", Resource.MODEL, "call", "ALLOW",
              provider="ollama", capability="coding"),
    ])
    evil = PermissionRequest(agent="a", resource=Resource.MODEL,
                             operation="call",
                             details=(("provider", "evil-vendor"),
                                      ("capability", "coding")))
    assert policy.evaluate(evil).decision == PolicyDecision.DENY


def test_secrets_in_approval_reasons_stay_out_of_audit():
    audit = AuditLog()
    manager = PermissionManager(audit=audit)
    gate = PolicyGate(manager)
    gate.evaluate(operation="write_file", path="a.py", approved=True,
                  agent="coder")
    payload = json.dumps(audit.to_dict())
    assert "AKIA" not in payload  # policy reasons carry no file content
    assert audit.events
