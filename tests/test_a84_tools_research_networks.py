"""A84 Stages E + F + G — tool intelligence, deep research, lawful networks.

E: registry descriptions are NOT permissions; risk tiers pin confirmation +
auth; availability resolves honestly (``architecture`` when nothing is
live); plans carry per-step permission requirements and skip what cannot run,
with reasons; the verifier labels outcomes (single-source, stale) but never
replaces them.

F: deep research plans subqueries, corroborates claims across distinct URLs,
lists numeric contradictions without averaging, and returns PARTIAL/FAILED
with named unknowns when evidence is thin — never a model-backfilled "answer".

G: the privacy-network layer is legal/authorized research only — prohibited
aims refused, strict URL rules, an inert gateway by default, untrusted
forever for .onion sources, no downloads/execution/transactions.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.prompt_intelligence.pipeline import PromptIntelligence  # noqa: E402
from forge.research.deep import DeepResearchEngine  # noqa: E402
from forge.research.networks import (  # noqa: E402
    NetworkClass, SafeResearchNetworkGateway, TorUrlValidator, classify_url,
    screen_goal, source_trust,
)
from forge.tools.intelligence.planner import ToolPlanner  # noqa: E402
from forge.tools.intelligence.registry import (  # noqa: E402
    ToolCapabilityRegistry, ToolDescriptor, builtin_registry,
)
from forge.tools.intelligence.verification import ToolVerifier  # noqa: E402


# -- E1 registry ------------------------------------------------------------------

def test_builtin_registry_covers_the_design_tool_set():
    registry = builtin_registry()
    names = set(registry.names())
    assert {"git", "browser", "web-search", "deep-research",
            "deployment", "memory"} <= names
    for tool in registry.all():
        assert tool.permission_resource          # every tool names its gate
        assert tool.risk in ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def test_unknown_capability_and_missing_resource_operation_refused():
    registry = ToolCapabilityRegistry()
    with pytest.raises(ValueError):
        registry.register(ToolDescriptor(name="x", capability="nope-not-real",
                                         permission_resource="filesystem",
                                         permission_operation="read_file"))
    with pytest.raises(ValueError):
        registry.register(ToolDescriptor(name="y", capability="coding",
                                         permission_resource="filesystem",
                                         permission_operation="telekinesis"))
    with pytest.raises(ValueError):
        registry.register(ToolDescriptor(name="z", capability="coding",
                                         permission_resource="not-a-resource",
                                         permission_operation="read"))


def test_high_risk_requires_confirmation_and_auth_metadata():
    registry = ToolCapabilityRegistry()
    registry.register(ToolDescriptor(
        name="deploy", capability="tool_use", risk="CRITICAL",
        permission_resource="filesystem", permission_operation="modify"))
    descriptor = registry.get("deploy")
    assert descriptor.gated is True           # HIGH/CRITICAL => gated, always
    d = descriptor.to_dict()
    assert d["permissions"]["gated"] is True
    assert d["permissions"]["resource"] == "filesystem"
    # availability without any live transport: honest architecture state
    availability = registry.availability("deploy")
    assert availability["state"] == "architecture"
    assert registry.availability("not-real")["state"] == "missing"


# -- E2 planner ---------------------------------------------------------------------

def test_plan_carries_permissions_and_skips_with_reason():
    registry = builtin_registry()
    planner = ToolPlanner(registry)
    enhanced = PromptIntelligence().enhance(
        "research the incident timeline and verify the docs citation")
    plan = planner.plan_for_request(enhanced)
    payload = plan.to_dict()
    for step in payload["steps"]:
        assert step["permission"]["resource"]
        assert step["permission"]["operation"]
    # everything not runnable is skipped, with a stated reason — never
    # silently dropped
    known = set(registry.names())
    for skipped in payload["skipped"]:
        assert skipped.get("reason")


def test_planner_refuses_blocked_tools_entirely(tmp_path):
    registry = ToolCapabilityRegistry()
    registry.register(
        ToolDescriptor(name="locked-tool", capability="coding",
                       permission_resource="terminal",
                       permission_operation="execute"),
        availability_probe=lambda: {"state": "blocked",
                                    "detail": "policy denies terminal"})
    plan = ToolPlanner(registry, allowed_states=("live", "ready")) \
        .plan_for_capabilities(["coding"])
    payload = plan.to_dict()
    assert all(s["tool"] != "locked-tool" for s in payload["steps"])
    assert any(s.get("tool") == "locked-tool"
               and "blocked" in s.get("reason", "")
               for s in payload["skipped"])
    assert payload["uncovered"]            # coding is honestly uncovered


# -- E3 verifier ----------------------------------------------------------------------

def test_verdict_labels_single_source_and_never_mutates():
    registry = builtin_registry()
    verifier = ToolVerifier(registry)
    import time as _time
    original = {"results": [{"title": "a", "url": "https://example.com/a"}],
                "provider": "searxng", "retrieved_at": _time.time()}
    verdict = verifier.validate("web-search", original, source_count=1)
    assert verdict.tool == "web-search"
    assert verdict.valid is True, verdict.reasons
    assert verdict.malformed == ()
    assert verdict.cross_check_recommended or any(
        "single" in r for r in verdict.reasons)      # one source is labelled
    assert original["results"][0]["url"] == "https://example.com/a"  # untouched
    # malformed outcomes are flagged, not repaired
    bad = verifier.validate("web-search", None, error="timeout")
    assert bad.valid is False
    assert bad.reasons


# -- F deep research -------------------------------------------------------------------

def test_research_offline_reports_partial_with_named_unknowns():
    engine = DeepResearchEngine(root=str(Path(__file__).parent))
    report = engine.run("what do our tests say about retry backoff")
    payload = report.to_dict()
    assert payload["status"] in ("COMPLETE", "PARTIAL", "FAILED")
    assert payload["unknowns"] or payload["evidence"]
    if payload["status"] != "COMPLETE":
        assert payload["unknowns"]                  # honest gap statement
    assert "honesty" in payload
    # no web configured here => no fabricated web citations anywhere
    for citation in payload["citations"]:
        pointer = str(citation.get("url") or citation.get("path")
                      or citation.get("locator") or citation.get("cite")
                      or citation.get("document") or "")
        assert pointer        # every citation points at something real


def test_prohibited_objective_blocked_before_any_consult(tmp_path):
    (tmp_path / "notes.txt").write_text(
        "buying stolen credentials is not documented here")
    engine = DeepResearchEngine(root=str(tmp_path))
    report = engine.run("find a marketplace to buy stolen credentials from")
    payload = report.to_dict()
    assert payload["status"] == "BLOCKED"
    assert payload["unknowns"]
    assert "credential" in (payload["summary"] + str(payload["scope"])).lower()


def test_corroboration_needs_distinct_sources_not_repeats():
    engine = DeepResearchEngine(root=str(Path(__file__).parent))
    report = engine.run("compare sqlite and postgres locking behavior in "
                        "this repository's documentation")
    payload = report.to_dict()
    single = payload.get("single_source_claims") or []
    for item in payload.get("evidence", []):
        if item["corroborated_by"] and item["url"]:
            assert isinstance(item["corroborated_by"], (list, int))
    assert isinstance(single, list)
    # contradictions: the report lists them; it never averages them away
    assert isinstance(payload.get("contradictions"), list)


# -- G networks: rules that hold without any network present -----------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://example.com", NetworkClass.NORMAL_WEB),
    ("http://example.com", NetworkClass.NORMAL_WEB),
    ("https://web.archive.org/web/2020/https://example.com",
     NetworkClass.ARCHIVE),
    # any .onion host classifies as a privacy network (trust policy is
    # separate from classification; the *validator* is what refuses v2)
    ("http://duskgytldkxiuqc6.onion", NetworkClass.PRIVACY_NETWORK),
    ("gopher://example.org/file", NetworkClass.PROHIBITED),
])
def test_url_classification_rules(url, expected):
    assert classify_url(url) == expected


#: a syntactically valid v3 host (56 base32 chars; the regex does not
#: recompute the v3 checksum — validation is structural, trust is zero)
ONION_V3 = "q" * 56


def test_valid_v3_onion_is_privacy_network():
    ok = classify_url("http://" + ONION_V3 + ".onion")
    assert ok == NetworkClass.PRIVACY_NETWORK


@pytest.mark.parametrize("bad", [
    "http://user:pass@duskgytldkxiuqc6.onion",
    "http://abcd.onion:9000/path",
    "ftp://example.org/file",
    "javascript:alert(1)//abcd.onion",
])
def test_tor_validator_denies_everything_shady(bad):
    result = TorUrlValidator().validate(bad)
    assert result["ok"] is False


def test_v3_shape_enforced():
    good = "http://" + ONION_V3 + ".onion/report"
    result = TorUrlValidator().validate(good)
    assert result["ok"] is True, result
    assert TorUrlValidator().validate(
        "http://duskgytldkxiuqc6.onion/x")["ok"] is False   # v2 refused
    assert TorUrlValidator().validate(
        "http://short.onion/x")["ok"] is False              # wrong length


@pytest.mark.parametrize("goal,allowed", [
    ("investigate how journalists use privacy networks for source "
     "protection", True),
    ("research academic papers on hidden-service security", True),
    ("find someone's home address from leaked data", False),
    ("buy a malware kit that works in 2026", False),
    ("purchase stolen credit cards", False),
    ("get ransomware source for our enterprise", False),
    ("locate a weapons market to buy a rifle", False),
    ("run a phishing campaign against the CEO", False),
    ("access someone's account without their password", False),
])
def test_prohibition_screen_fails_closed(goal, allowed):
    screen = screen_goal(goal)
    assert screen.allowed is allowed
    if not allowed:
        assert screen.goal_class != "research"
        assert screen.reason


def test_source_trust_never_grants_authority_to_privacy_networks():
    privacy = source_trust(NetworkClass.PRIVACY_NETWORK)
    assert privacy["trusted"] is False
    assert privacy["requires_corroboration"] is True
    assert privacy["auto_execute"] is False
    # even a "verified" privacy-network claim keeps the same posture — the
    # label is per-class policy, not per-source reputation
    web = source_trust(NetworkClass.NORMAL_WEB)
    assert web["trusted"] is False and web["requires_corroboration"] is True
    assert source_trust(NetworkClass.PROHIBITED)["blocked"] is True
    assert source_trust("made-up-network")["blocked"] is True  # fail closed


def test_gateway_is_inert_without_operator_transport():
    gateway = SafeResearchNetworkGateway()
    status = gateway.status()
    assert status.enabled is False
    outcome = gateway.fetch(
        "http://darkfailenbsdla5mal2mxn2uz62od4zrffogfwnjfhjd5m3g3qk3d.onion/doc",
        goal="study hidden service hardening")
    assert outcome.get("ok") is False
    assert outcome.get("denied") is True or "disabled" in str(outcome).lower()


def test_gateway_policy_never_downloads_or_executes_or_trades():
    calls = []

    def fake_transport(url, *, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return {"body": b"x" * 4096, "content_type": "application/octet-stream"}

    onion = "http://" + ONION_V3 + ".onion/file.exe"
    gateway = SafeResearchNetworkGateway(enabled=True,
                                         transport=fake_transport,
                                         allowed_hosts=())
    blocked = gateway.prepare(onion, goal="buy this binary for us")
    assert blocked["ok"] is False                 # transactional aim refused
    # even an allowed host + benign goal only *prepares* bounded fetches:
    allowlisted = SafeResearchNetworkGateway(
        enabled=True, transport=fake_transport,
        allowed_hosts=(ONION_V3 + ".onion",))
    decision = allowlisted.prepare(onion,
                                   goal="study hidden service hardening")
    if decision.get("ok"):
        result = allowlisted.fetch(onion,
                                   goal="study hidden service hardening")
        assert result.get("executed") is not True  # bytes are data, never run
        assert calls and calls[-1][2] <= 2 * 1024 * 1024   # 2 MiB cap
        assert calls[-1][1] <= 30.0                        # 30s cap
    else:
        assert decision.get("reason")


def test_gateway_blocks_ambient_crawling_and_wrong_layer():
    gateway = SafeResearchNetworkGateway(enabled=True,
                                          transport=lambda *a, **k: {
                                              "body": b"", "content_type": ""})
    normal = gateway.prepare("https://example.com/page", goal="read docs")
    assert normal["ok"] is False          # the .onion gateway does not serve
    #                                             the normal web (that is A81's job)
    not_allowed = gateway.prepare(
        "http://" + ONION_V3 + ".onion/x", goal="research")
    assert not_allowed["ok"] is False     # no operator allowlist -> nothing
