"""Browser, network, desktop, and voice foundation tests (A33)."""
import sys

from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRule, Resource
from forge.security.policy_gate import PolicyDecision
from forge.tools.browser import MockBrowser
from forge.tools.desktop import (
    DesktopAction,
    DesktopActionRequest,
    DesktopResource,
    MockDesktop,
)
from forge.tools.network import MockNetwork
from forge.voice import VoiceCommand, VoiceInterface


def _policy(*rules):
    return PermissionPolicy(rules=list(rules))


def _rule(rule_id, resource, operation, effect, scope="", **kwargs):
    return PermissionRule(rule_id, resource, operation, effect, scope,
                          reason="test", **kwargs)


# -- browser ----------------------------------------------------------------------

def test_browser_allows_listed_site_and_denies_unknown():
    browser = MockBrowser(_policy(
        _rule("nav", Resource.BROWSER, "navigate", "ALLOW", "example.com")))
    browser.serve("https://example.com/", "<h1>hi</h1>")
    good = browser.navigate("https://example.com/", agent="BrowserAgent")
    assert good.allowed and good.content == "<h1>hi</h1>"
    evil = browser.navigate("https://unauthorized.example/", agent="BrowserAgent")
    assert not evil.allowed and evil.decision == "DENY"
    assert [visit["url"] for visit in browser.visits] == ["https://example.com/"]


def test_browser_submit_requires_approval_then_token():
    store = ApprovalStore()
    browser = MockBrowser(_policy(
        _rule("nav", Resource.BROWSER, "navigate", "ALLOW", "example.com"),
        _rule("sub", Resource.BROWSER, "submit", "REQUIRE_APPROVAL", "example.com"),
    ), store=store)
    first = browser.submit("https://example.com/form", agent="BrowserAgent")
    assert not first.allowed and first.approval_required
    assert first.approval_request_id
    store.decide(first.approval_request_id, True, "operator")
    token = store.issue(first.approval_request_id, "operator")
    second = browser.submit("https://example.com/form", agent="BrowserAgent",
                            approval_token_id=token.id)
    assert second.allowed
    # The token authorized example.com only.
    third = browser.submit("https://other.example/form", agent="BrowserAgent",
                           approval_token_id=token.id)
    assert not third.allowed


def test_browser_without_policy_denies_everything():
    browser = MockBrowser()
    assert not browser.navigate("https://example.com/", agent="a").allowed
    assert browser.visits == []


# -- network ----------------------------------------------------------------------

def test_network_allows_listed_https_and_denies_unknown():
    network = MockNetwork(_policy(
        _rule("api", Resource.NETWORK, "request", "ALLOW", "api.example.com",
              port=443, protocol="https")))
    network.serve("api.example.com", '{"ok": true}')
    good = network.request("api.example.com", 443, "https", agent="ResearchAgent")
    assert good.allowed and good.response == '{"ok": true}'
    assert network.request("unknown.example", 443, "https", agent="a").decision == "DENY"
    assert network.request("api.example.com", 80, "https", agent="a").decision == "DENY"
    assert len(network.calls) == 1


def test_network_approval_flow_binds_port_and_protocol():
    store = ApprovalStore()
    network = MockNetwork(_policy(
        _rule("api", Resource.NETWORK, "request", "REQUIRE_APPROVAL",
              "api.example.com")),
        store=store)
    first = network.request("api.example.com", 443, "https", agent="a")
    assert first.approval_required and first.approval_request_id
    store.decide(first.approval_request_id, True, "operator")
    token = store.issue(first.approval_request_id, "operator", max_uses=2)
    assert token.bind == (("port", 443), ("protocol", "https"))
    assert network.request("api.example.com", 443, "https", agent="a",
                           approval_token_id=token.id).allowed
    rebound = network.request("api.example.com", 22, "tcp", agent="a",
                              approval_token_id=token.id)
    assert not rebound.allowed


def test_network_check_is_side_effect_free():
    network = MockNetwork(_policy(
        _rule("api", Resource.NETWORK, "request", "ALLOW", "api.example.com")))
    evaluation = network.check("api.example.com", 443, "https", agent="a")
    assert evaluation.decision == PolicyDecision.ALLOW
    assert network.calls == []


# -- desktop ----------------------------------------------------------------------

def test_desktop_read_allowed_keyboard_gated_launch_denied():
    store = ApprovalStore()
    desktop = MockDesktop(_policy(
        _rule("screen", Resource.DESKTOP, "read_screen", "ALLOW"),
        _rule("keys", Resource.DESKTOP, "keyboard", "REQUIRE_APPROVAL"),
    ), store=store)
    screen = DesktopActionRequest(DesktopAction.READ_SCREEN,
                                  DesktopResource("screen", "main"),
                                  agent="DesktopAgent")
    assert desktop.perform(screen).allowed
    keys = DesktopActionRequest(DesktopAction.KEYBOARD,
                                DesktopResource("application", "editor"),
                                agent="DesktopAgent")
    gated = desktop.perform(keys)
    assert not gated.allowed and gated.approval_required
    store.decide(gated.approval_request_id, True, "operator")
    token = store.issue(gated.approval_request_id, "operator")
    assert desktop.perform(keys, approval_token_id=token.id).allowed
    launch = DesktopActionRequest(DesktopAction.LAUNCH,
                                  DesktopResource("application", "shell"),
                                  agent="DesktopAgent")
    assert desktop.perform(launch).decision == "DENY"
    assert [action["action"] for action in desktop.actions] == [
        "read_screen", "keyboard"]


def test_desktop_check_evaluates_without_performing():
    desktop = MockDesktop(_policy(
        _rule("screen", Resource.DESKTOP, "read_screen", "ALLOW")))
    request = DesktopActionRequest("read_screen", DesktopResource("screen"),
                                   agent="a")
    permission = desktop.check(request)
    assert permission.allowed and desktop.actions == []
    assert permission.to_dict()["decision"] == "ALLOW"


def test_mocks_import_no_real_control_libraries():
    MockBrowser()
    MockDesktop()
    MockNetwork()
    VoiceInterface()
    for module in ("pynput", "pyautogui", "selenium", "playwright", "pyaudio"):
        assert module not in sys.modules


# -- voice --------------------------------------------------------------------------

def test_voice_command_creates_task_when_allowed():
    voice = VoiceInterface(_policy(
        _rule("update", Resource.VOICE, "command", "ALLOW", "update_website")))
    result = voice.handle(VoiceCommand("Forge, update the website."))
    assert result.ok and result.task is not None
    assert result.intent.name == "update_website"
    assert result.task["intent"] == "update_website"


def test_voice_unknown_intent_denied_without_policy_bypass():
    voice = VoiceInterface(_policy(
        _rule("any", Resource.VOICE, "command", "ALLOW")))
    result = voice.handle(VoiceCommand("Forge, launch the missiles."))
    assert not result.ok
    assert result.intent.name == "unknown"
    assert result.task is None
    assert result.permission.decision == PolicyDecision.DENY


def test_voice_denied_intent_creates_no_task():
    voice = VoiceInterface()
    result = voice.handle(VoiceCommand("run tests"))
    assert not result.ok and result.task is None


def test_voice_approval_flow():
    store = ApprovalStore()
    voice = VoiceInterface(_policy(
        _rule("commit", Resource.VOICE, "command", "REQUIRE_APPROVAL", "commit")),
        store=store)
    first = voice.handle(VoiceCommand("commit changes"))
    assert not first.ok and first.permission.approval_required
    store.decide(first.permission.approval_request_id, True, "operator")
    token = store.issue(first.permission.approval_request_id, "operator")
    second = voice.handle(VoiceCommand("commit changes"),
                          approval_token_id=token.id)
    assert second.ok and second.task is not None


def test_voice_slots_parsed_deterministically():
    voice = VoiceInterface()
    intent, _ = voice.check(VoiceCommand("summarize quarterly report"))
    assert intent.name == "summarize"
    assert intent.slots == {"target": "quarterly report"}


# -- shared audit -----------------------------------------------------------------------

def test_resource_actions_share_one_audit_log():
    audit = AuditLog()
    policy = _policy(
        _rule("nav", Resource.BROWSER, "navigate", "ALLOW", "example.com"),
        _rule("net", Resource.NETWORK, "request", "ALLOW", "api.example.com"))
    MockBrowser(policy, audit=audit).navigate(
        "https://example.com/", agent="b", task_id="t")
    MockNetwork(policy, audit=audit).request(
        "api.example.com", 443, "https", agent="n", task_id="t")
    assert [event.resource for event in audit.query(task_id="t")] == [
        "browser", "network"]
