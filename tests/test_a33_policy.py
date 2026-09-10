"""Policy engine tests (A33): rules, precedence, scopes, simulation, config."""

import pytest

from forge.security.policy import (
    PermissionEvaluation,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    Resource,
    assisted_profile,
    autonomous_profile,
    custom_profile,
    locked_profile,
    most_restrictive,
    safe_profile,
    scope_for_a32,
    translate_a32,
)
from forge.security.policy_gate import PolicyDecision


def _rule(rule_id, resource, operation, effect, scope="", **kwargs):
    return PermissionRule(rule_id, resource, operation, effect, scope,
                          reason="test", **kwargs)


def _request(agent, resource, operation, scope="", **kwargs):
    return PermissionRequest(agent=agent, resource=resource,
                             operation=operation, scope=scope, **kwargs)


# -- request model ------------------------------------------------------------

def test_request_carries_who_what_where_when_how_risk_why():
    request = PermissionRequest(
        agent="Forge/CoderAgent", resource=Resource.FILESYSTEM,
        operation="write", scope="src/app.py", risk="low",
        reason="implement feature", task_id="t1", trace_id="trace",
        details=(("capability", "coding"),))
    assert request.agent == "Forge/CoderAgent"
    assert request.operation == "write"
    assert request.scope == "src/app.py"
    assert request.risk == "LOW"
    assert request.reason == "implement feature"
    assert request.detail("capability") == "coding"
    assert request.timestamp > 0
    assert request.request_id


def test_unknown_resource_rejected():
    with pytest.raises(ValueError):
        PermissionRequest(agent="a", resource="teleport", operation="go")


def test_operation_normalized_to_lowercase():
    request = _request("a", Resource.GIT, "COMMIT")
    assert request.operation == "commit"


# -- filesystem scopes ----------------------------------------------------------

def test_filesystem_scope_shapes():
    policy = PermissionPolicy(rules=[
        _rule("exact", Resource.FILESYSTEM, "read", "ALLOW", "src/app.py"),
        _rule("children", Resource.FILESYSTEM, "read", "ALLOW", "docs/*"),
        _rule("recursive", Resource.FILESYSTEM, "read", "ALLOW", "src/**"),
        _rule("slash", Resource.FILESYSTEM, "read", "ALLOW", "gen/"),
    ])
    assert policy.evaluate(_request("a", "filesystem", "read", "src/app.py")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "read", "src/deep/x.py")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "read", "docs/a.md")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "read", "gen/x.py")).decision == PolicyDecision.ALLOW
    # Direct-children pattern does not descend; unrelated paths fail closed.
    assert policy.evaluate(_request("a", "filesystem", "read", "docs/a/b.md")).decision == PolicyDecision.DENY
    assert policy.evaluate(_request("a", "filesystem", "read", "other/x.py")).decision == PolicyDecision.DENY


@pytest.mark.parametrize("unsafe", [
    "/etc/passwd", "../escape.py", "a/../../b.py", "a\\b.py", "",
])
def test_unsafe_filesystem_scopes_never_match(unsafe):
    policy = PermissionPolicy(rules=[
        _rule("all", Resource.FILESYSTEM, "read", "ALLOW", "**")])
    evaluation = policy.evaluate(_request("a", "filesystem", "read", unsafe))
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.default_applied


# -- precedence and specificity ---------------------------------------------------

def test_specific_deny_beats_broad_allow():
    policy = PermissionPolicy(rules=[
        _rule("broad", Resource.FILESYSTEM, "write", "ALLOW", "project/**"),
        _rule("secret", Resource.FILESYSTEM, "write", "DENY", "project/secrets/**"),
    ])
    evaluation = policy.evaluate(_request("a", "filesystem", "write", "project/secrets/k"))
    assert evaluation.decision == PolicyDecision.DENY
    assert [rule.id for rule in evaluation.matched_rules] == ["secret"]
    evaluation = policy.evaluate(_request("a", "filesystem", "write", "project/src/a"))
    assert evaluation.decision == PolicyDecision.ALLOW


def test_exact_allow_beats_recursive_deny():
    policy = PermissionPolicy(rules=[
        _rule("rec-deny", Resource.FILESYSTEM, "write", "DENY", "src/**"),
        _rule("exact-allow", Resource.FILESYSTEM, "write", "ALLOW", "src/app.py"),
    ])
    evaluation = policy.evaluate(_request("a", "filesystem", "write", "src/app.py"))
    assert evaluation.decision == PolicyDecision.ALLOW


def test_equal_specificity_deny_wins_over_allow():
    policy = PermissionPolicy(rules=[
        _rule("a-allow", Resource.GIT, "commit", "ALLOW"),
        _rule("b-deny", Resource.GIT, "commit", "DENY"),
    ])
    evaluation = policy.evaluate(_request("a", "git", "commit"))
    assert evaluation.decision == PolicyDecision.DENY
    assert {rule.id for rule in evaluation.matched_rules} == {"a-allow", "b-deny"}


def test_require_approval_beats_allow_at_equal_specificity():
    policy = PermissionPolicy(rules=[
        _rule("a-allow", Resource.GIT, "push", "ALLOW"),
        _rule("b-gate", Resource.GIT, "push", "REQUIRE_APPROVAL"),
    ])
    assert policy.evaluate(_request("a", "git", "push")).decision == PolicyDecision.REQUIRE_APPROVAL


def test_agent_constrained_rule_beats_generic_rule():
    policy = PermissionPolicy(rules=[
        _rule("generic", Resource.FILESYSTEM, "read", "DENY", "src/**"),
        _rule("coder", Resource.FILESYSTEM, "read", "ALLOW", "src/**", agent="coder"),
    ])
    assert policy.evaluate(_request("coder", "filesystem", "read", "src/a")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("other", "filesystem", "read", "src/a")).decision == PolicyDecision.DENY


def test_task_bound_rule_only_matches_its_task():
    policy = PermissionPolicy(rules=[
        _rule("task", Resource.FILESYSTEM, "write", "ALLOW", "src/**", task_id="t1"),
    ])
    assert policy.evaluate(_request("a", "filesystem", "write", "src/a", task_id="t1")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "src/a", task_id="t2")).decision == PolicyDecision.DENY


def test_risk_ceiling_filters_high_risk():
    policy = PermissionPolicy(rules=[
        _rule("low", Resource.FILESYSTEM, "write", "ALLOW", "src/**", risk_ceiling="MEDIUM"),
    ])
    assert policy.evaluate(_request("a", "filesystem", "write", "src/a", risk="LOW")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "src/a", risk="HIGH")).decision == PolicyDecision.DENY


def test_unknown_risk_ranks_high_fail_closed():
    policy = PermissionPolicy(rules=[
        _rule("low", Resource.FILESYSTEM, "write", "ALLOW", "src/**", risk_ceiling="MEDIUM"),
    ])
    evaluation = policy.evaluate(_request("a", "filesystem", "write", "src/a", risk="bogus"))
    assert evaluation.decision == PolicyDecision.DENY


def test_time_window_enforced_deterministically():
    policy = PermissionPolicy(rules=[
        _rule("window", Resource.FILESYSTEM, "write", "ALLOW", "src/**",
              valid_from=1000.0, valid_until=2000.0),
    ])
    request = _request("a", "filesystem", "write", "src/a")
    assert policy.evaluate(request, now=999.0).decision == PolicyDecision.DENY
    assert policy.evaluate(request, now=1000.0).decision == PolicyDecision.ALLOW
    assert policy.evaluate(request, now=1999.0).decision == PolicyDecision.ALLOW
    assert policy.evaluate(request, now=2000.0).decision == PolicyDecision.DENY


def test_no_match_applies_default_deny_fail_closed():
    policy = PermissionPolicy()
    evaluation = policy.evaluate(_request("a", "browser", "navigate", "https://x.example/"))
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.default_applied
    assert evaluation.matched_rules == ()


# -- browser / network ------------------------------------------------------------

def test_browser_exact_and_wildcard_hosts():
    policy = PermissionPolicy(rules=[
        _rule("apex", Resource.BROWSER, "navigate", "ALLOW", "example.com"),
        _rule("wild", Resource.BROWSER, "read", "ALLOW", "*.example.com"),
    ])
    assert policy.evaluate(_request("b", "browser", "navigate", "https://example.com/a")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("b", "browser", "read", "https://docs.example.com/a")).decision == PolicyDecision.ALLOW
    # Apex is not covered by the subdomain wildcard.
    assert policy.evaluate(_request("b", "browser", "read", "https://example.com/a")).decision == PolicyDecision.DENY
    # Unknown sites fail closed.
    assert policy.evaluate(_request("b", "browser", "navigate", "https://evil.example/a")).decision == PolicyDecision.DENY


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)", "file:///etc/passwd", "https://user:pw@example.com/",
    "not a url", "ftp://example.com/x",
])
def test_browser_rejects_unsafe_urls(bad_url):
    policy = PermissionPolicy(rules=[
        _rule("apex", Resource.BROWSER, "navigate", "ALLOW", "example.com")])
    assert policy.evaluate(_request("b", "browser", "navigate", bad_url)).decision == PolicyDecision.DENY


def test_network_host_port_protocol_matching():
    policy = PermissionPolicy(rules=[
        _rule("api", Resource.NETWORK, "request", "ALLOW", "api.example.com",
              port=443, protocol="https"),
    ])
    good = _request("a", "network", "request", details=(
        ("host", "api.example.com"), ("port", 443), ("protocol", "https")))
    assert policy.evaluate(good).decision == PolicyDecision.ALLOW
    wrong_port = _request("a", "network", "request", details=(
        ("host", "api.example.com"), ("port", 80), ("protocol", "https")))
    assert policy.evaluate(wrong_port).decision == PolicyDecision.DENY
    unknown_host = _request("a", "network", "request", details=(
        ("host", "unknown.example"), ("port", 443), ("protocol", "https")))
    assert policy.evaluate(unknown_host).decision == PolicyDecision.DENY


# -- terminal ---------------------------------------------------------------------

def test_terminal_requires_exact_pinned_args():
    policy = PermissionPolicy(rules=[
        _rule("pytest", Resource.TERMINAL, "execute", "ALLOW", "pytest",
              args=("-q", "tests/")),
    ])
    good = _request("a", "terminal", "execute", "pytest",
                    details=(("args", ("-q", "tests/")),))
    assert policy.evaluate(good).decision == PolicyDecision.ALLOW
    extra_arg = _request("a", "terminal", "execute", "pytest",
                         details=(("args", ("-q", "tests/", "--evil")),))
    assert policy.evaluate(extra_arg).decision == PolicyDecision.DENY
    other_binary = _request("a", "terminal", "execute", "rm",
                            details=(("args", ("-q", "tests/")),))
    assert policy.evaluate(other_binary).decision == PolicyDecision.DENY


def test_terminal_unqualified_rule_matches_basename():
    policy = PermissionPolicy(rules=[
        _rule("pytest", Resource.TERMINAL, "execute", "ALLOW",
              "/usr/bin/pytest", args=("-q",))])
    request = _request("a", "terminal", "execute", "/usr/bin/pytest",
                       details=(("args", ("-q",)),))
    assert policy.evaluate(request).decision == PolicyDecision.ALLOW


def test_terminal_catch_all_deny_matches_any_executable():
    policy = PermissionPolicy(rules=[
        _rule("noexec", Resource.TERMINAL, "execute", "DENY", "**")])
    assert policy.evaluate(_request("a", "terminal", "execute", "rm",
                                    details=(("args", ("-rf", "/")),))).decision == PolicyDecision.DENY


def test_terminal_allow_without_pinned_args_rejected():
    with pytest.raises(ValueError, match="must pin"):
        _rule("broad", Resource.TERMINAL, "execute", "ALLOW", "pytest")


def test_terminal_allow_with_any_executable_rejected():
    with pytest.raises(ValueError, match="must pin"):
        _rule("broad", Resource.TERMINAL, "execute", "ALLOW", "**", args=("-q",))


# -- model / git / desktop / voice --------------------------------------------------

def test_model_provider_capability_constraints():
    policy = PermissionPolicy(rules=[
        _rule("local-code", Resource.MODEL, "call", "ALLOW",
              provider="ollama", capability="coding"),
        _rule("no-vendor", Resource.MODEL, "call", "DENY",
              provider="unauthorized-vendor"),
    ])
    good = _request("a", "model", "call", details=(
        ("provider", "ollama"), ("model", "ollama/q"), ("capability", "coding")))
    assert policy.evaluate(good).decision == PolicyDecision.ALLOW
    bad_vendor = _request("a", "model", "call", details=(
        ("provider", "unauthorized-vendor"), ("capability", "coding")))
    assert policy.evaluate(bad_vendor).decision == PolicyDecision.DENY
    other_capability = _request("a", "model", "call", details=(
        ("provider", "ollama"), ("capability", "vision")))
    assert policy.evaluate(other_capability).decision == PolicyDecision.DENY


def test_git_desktop_voice_matching():
    policy = PermissionPolicy(rules=[
        _rule("commit", Resource.GIT, "commit", "REQUIRE_APPROVAL"),
        _rule("screen", Resource.DESKTOP, "read_screen", "ALLOW"),
        _rule("keys", Resource.DESKTOP, "keyboard", "REQUIRE_APPROVAL"),
        _rule("intent", Resource.VOICE, "command", "ALLOW", scope="summarize"),
    ])
    assert policy.evaluate(_request("a", "git", "commit")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "desktop", "read_screen")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "desktop", "keyboard")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "voice", "command", "summarize")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "voice", "command", "format")).decision == PolicyDecision.DENY


# -- rule validation ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"resource": Resource.FILESYSTEM, "operation": "teleport"},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": ""},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": "/abs"},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": "../x"},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": "a/**/b"},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": "a/*/b"},
    {"resource": Resource.FILESYSTEM, "operation": "read", "scope": "mid*dle"},
    {"resource": Resource.BROWSER, "operation": "read", "scope": ""},
    {"resource": Resource.BROWSER, "operation": "read", "scope": "*.com"},
    {"resource": Resource.BROWSER, "operation": "read", "scope": "exa mple.com"},
    {"resource": Resource.BROWSER, "operation": "read", "scope": "https://example.com/"},
    {"resource": Resource.TERMINAL, "operation": "execute", "scope": ""},
    {"resource": Resource.GIT, "operation": "commit", "args": ("x",)},
    {"resource": Resource.GIT, "operation": "commit", "provider": "p"},
    {"resource": Resource.GIT, "operation": "commit", "port": 80},
    {"resource": Resource.NETWORK, "operation": "request", "scope": "h.example", "port": 0},
    {"resource": Resource.NETWORK, "operation": "request", "scope": "h.example", "protocol": "gopher"},
    {"resource": Resource.GIT, "operation": "commit", "risk_ceiling": "bogus"},
    {"resource": Resource.GIT, "operation": "commit", "valid_from": 5.0, "valid_until": 4.0},
])
def test_invalid_rules_rejected(kwargs):
    with pytest.raises(ValueError):
        PermissionRule("bad", kwargs.pop("resource"), kwargs.pop("operation"),
                       PolicyDecision.DENY, **kwargs)


def test_duplicate_rule_id_rejected():
    policy = PermissionPolicy()
    policy.add_rule(_rule("dup", Resource.GIT, "status", "ALLOW"))
    with pytest.raises(ValueError, match="Duplicate rule id"):
        policy.add_rule(_rule("dup", Resource.GIT, "diff", "ALLOW"))


def test_remove_rule():
    policy = PermissionPolicy(rules=[
        _rule("gone", Resource.GIT, "status", "ALLOW")])
    assert policy.remove_rule("gone") is True
    assert policy.remove_rule("gone") is False
    assert policy.evaluate(_request("a", "git", "status")).decision == PolicyDecision.DENY


# -- simulation / cache ------------------------------------------------------------------

def test_simulate_returns_dry_run_shape_without_side_effects():
    policy = PermissionPolicy(rules=[
        _rule("r", Resource.GIT, "commit", "REQUIRE_APPROVAL")])
    version = policy.version
    result = policy.simulate(_request("a", "git", "commit"))
    assert result["decision"] == "REQUIRE_APPROVAL"
    assert result["matched_rules"][0]["id"] == "r"
    assert result["risk"] == "NONE"
    assert result["scope"] == ""
    assert "Rule r" in result["reason"]
    assert policy.version == version
    assert len(policy.rules) == 1


def test_decision_cache_invalidated_on_policy_change():
    policy = PermissionPolicy()
    request = _request("a", "git", "status")
    first = policy.evaluate(request)
    assert policy.evaluate(request) is first  # cache hit
    policy.add_rule(_rule("now", Resource.GIT, "status", "ALLOW"))
    second = policy.evaluate(request)
    assert second is not first
    assert second.decision == PolicyDecision.ALLOW


def test_time_bound_rules_skip_cache():
    policy = PermissionPolicy(rules=[
        _rule("window", Resource.GIT, "status", "ALLOW",
              valid_from=1000.0, valid_until=2000.0)])
    request = _request("a", "git", "status")
    assert policy.evaluate(request, now=1500.0).decision == PolicyDecision.ALLOW
    assert policy.evaluate(request, now=2500.0).decision == PolicyDecision.DENY
    assert policy.evaluate(request, now=1500.0).decision == PolicyDecision.ALLOW


# -- config ------------------------------------------------------------------

def test_config_round_trip():
    policy = PermissionPolicy.from_dict({
        "version": 1,
        "default": "deny",
        "rules": [
            {"id": "fs", "resource": "filesystem", "operation": "read",
             "effect": "allow", "scope": "src/**", "reason": "code review"},
            {"id": "term", "resource": "terminal", "operation": "execute",
             "effect": "allow", "scope": "pytest", "args": ["-q"]},
            {"id": "net", "resource": "network", "operation": "request",
             "effect": "allow", "scope": "api.example.com", "port": 443,
             "protocol": "https", "valid_until": 4102444800},
        ],
    })
    assert policy.evaluate(_request("a", "filesystem", "read", "src/x")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "terminal", "execute", "pytest",
                                    details=(("args", ("-q",)),))).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "network", "request", details=(
        ("host", "api.example.com"), ("port", 443), ("protocol", "https")),
    ), now=1700000000).decision == PolicyDecision.ALLOW
    rebuilt = PermissionPolicy.from_dict(policy.to_dict())
    assert [rule.id for rule in rebuilt.rules] == ["fs", "term", "net"]
    assert rebuilt.default == PolicyDecision.DENY


@pytest.mark.parametrize("config", [
    {"version": 2, "rules": []},
    {"default": "maybe", "rules": []},
    {"rules": {}},
    {"rules": [{"id": "x", "resource": "git", "operation": "status"}]},
    {"rules": [{"id": "x", "resource": "git", "operation": "status",
                "effect": "allow", "bogus": 1}]},
    {"rules": [
        {"id": "x", "resource": "git", "operation": "status", "effect": "allow"},
        {"id": "x", "resource": "git", "operation": "diff", "effect": "allow"}]},
    {"rules": [{"id": "x", "resource": "git", "operation": "status",
                "effect": "allow", "scope": None}]},
    {"rules": [{"id": "x", "resource": "terminal", "operation": "execute",
                "effect": "allow", "scope": "pytest"}]},
    {"rules": [{"id": "x", "resource": "browser", "operation": "read",
                "effect": "allow", "scope": "*.com"}]},
    {"rules": [{"id": "x", "resource": "network", "operation": "request",
                "effect": "allow", "scope": "h.example", "port": 99999}]},
    {"rules": [{"id": "x", "resource": "git", "operation": "commit",
                "effect": "allow", "valid_until": "soon"}]},
    {"mystery": True},
    "not-a-mapping",
])
def test_invalid_config_rejected_atomically(config):
    with pytest.raises(ValueError):
        PermissionPolicy.from_dict(config)


# -- profiles ------------------------------------------------------------------

def test_locked_profile_read_only():
    policy = locked_profile()
    assert policy.evaluate(_request("a", "filesystem", "read", "x.py")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "x.py")).decision == PolicyDecision.DENY
    assert policy.evaluate(_request("a", "browser", "navigate", "https://example.com/")).decision == PolicyDecision.DENY


def test_safe_profile_with_read_lists():
    policy = safe_profile(allowed_sites=("docs.example.com",),
                          allowed_hosts=("api.example.com",))
    assert policy.evaluate(_request("a", "browser", "read",
                                    "https://docs.example.com/")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "browser", "read",
                                    "https://other.example/")).decision == PolicyDecision.DENY
    assert policy.evaluate(_request("a", "network", "request", details=(
        ("host", "api.example.com"), ("port", 443),
        ("protocol", "https")))).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "x")).decision == PolicyDecision.DENY


def test_assisted_profile_gates_mutations():
    policy = assisted_profile(allowed_sites=("example.com",))
    assert policy.evaluate(_request("a", "filesystem", "read", "x")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "x")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "terminal", "execute", "pytest",
                                    details=(("args", ("-q",)),))).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "git", "commit")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "browser", "navigate",
                                    "https://example.com/")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "browser", "submit",
                                    "https://example.com/")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "browser", "navigate",
                                    "https://unknown.example/")).decision == PolicyDecision.DENY


def test_autonomous_profile_auto_allows_low_risk_only():
    policy = autonomous_profile()
    assert policy.evaluate(_request("a", "filesystem", "write", "x", risk="LOW")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "filesystem", "write", "x", risk="HIGH")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "filesystem", "delete", "x", risk="LOW")).decision == PolicyDecision.REQUIRE_APPROVAL
    assert policy.evaluate(_request("a", "git", "commit")).decision == PolicyDecision.REQUIRE_APPROVAL


def test_custom_profile():
    policy = custom_profile([
        _rule("only", Resource.GIT, "status", "ALLOW")])
    assert policy.evaluate(_request("a", "git", "status")).decision == PolicyDecision.ALLOW
    assert policy.evaluate(_request("a", "git", "diff")).decision == PolicyDecision.DENY


# -- helpers ------------------------------------------------------------------

def test_most_restrictive_ordering():
    assert most_restrictive(PolicyDecision.ALLOW, PolicyDecision.DENY) == PolicyDecision.DENY
    assert most_restrictive(PolicyDecision.ALLOW, PolicyDecision.REQUIRE_APPROVAL) == PolicyDecision.REQUIRE_APPROVAL
    assert most_restrictive(PolicyDecision.REQUIRE_APPROVAL, PolicyDecision.DENY) == PolicyDecision.DENY
    assert most_restrictive(PolicyDecision.ALLOW, PolicyDecision.ALLOW) == PolicyDecision.ALLOW


def test_translate_a32_operations():
    assert translate_a32("write_file") == (Resource.FILESYSTEM, "write")
    assert translate_a32("run_command") == (Resource.TERMINAL, "execute")
    assert translate_a32("git_commit") == (Resource.GIT, "commit")
    assert translate_a32("mystery") is None


def test_scope_for_a32_calls():
    assert scope_for_a32("write_file", {"path": "a.py"}) == "a.py"
    assert scope_for_a32("run_command", {"command": ["pytest", "-q"]}) == "pytest"
    assert scope_for_a32("git_commit", {}) == ""


def test_evaluation_serializes():
    evaluation = PermissionEvaluation(
        decision=PolicyDecision.ALLOW, reason="ok", risk="LOW", scope="x",
        request_id="r")
    assert evaluation.to_dict()["decision"] == "ALLOW"


def test_request_serializes_with_details():
    request = _request("a", "network", "request",
                       details=(("host", "h"), ("port", 443)))
    assert request.to_dict()["details"] == {"host": "h", "port": 443}
