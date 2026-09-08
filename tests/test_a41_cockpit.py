"""Premium cockpit (A41): catalog surfaces, themes, responsive CSS."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

A41_VIEWS = ("agents", "security", "settings")
A41_HOOKS = ("agents-list", "security-posture", "security-invariants",
             "settings-session", "settings-theme", "settings-theme-note")


def _read(name: str) -> str:
    return (WEB / name).read_text()


def test_catalog_endpoints_live(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=PermissionPolicy(rules=[
        PermissionRule(id="v1", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW")]))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/agents").status_code == 401
        assert client.get("/api/v1/security").status_code == 401
        _session, _token, headers = login(client)
        agents = client.get("/api/v1/agents", headers=headers)
        assert agents.status_code == 200
        catalog = agents.json()["agents"]
        names = {agent["name"] for agent in catalog}
        for expected in ("planner", "coder", "debugger", "tester",
                         "reviewer", "security", "researcher",
                         "forge-orchestrator", "forge-voice",
                         "forge-vision", "forge-computer",
                         "forge-desktop"):
            assert expected in names, expected
        for agent in catalog:
            assert agent["role"]
            assert agent["gate"]
            assert agent["real"] is True
        posture = client.get("/api/v1/security", headers=headers)
        assert posture.status_code == 200
        payload = posture.json()
        assert payload["mode"] == "assisted"
        assert payload["policy_default"] == "DENY"
        assert payload["hard_invariants"]
        assert "no policy means no authority" in \
            " ".join(payload["hard_invariants"]).lower() or any(
                "policy" in item for item in payload["hard_invariants"])
        # Simulation honesty is labeled in the posture.
        assert payload["vision_simulation"] is True
        assert payload["voice_simulation"] is True


def test_a41_views_routes_and_hooks():
    html = _read("index.html")
    js = _read("app.js")
    for view in A41_VIEWS:
        assert f'<template id="tpl-{view}">' in html, view
        assert f'href="#/{view}"' in html, view
    for hook in A41_HOOKS:
        assert f'id="{hook}"' in html, hook
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    for view in A41_VIEWS:
        assert view in routes_block, view
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    for view in A41_VIEWS:
        assert f"Go to {view.title()}" in palette, view
        assert f"#/{view}" in palette, view


def test_theme_support_is_real():
    css = _read("styles.css")
    js = _read("app.js")
    html = _read("index.html")
    assert '[data-theme="light"]' in css
    assert "max-width: 760px" in css
    assert "flex-direction: column" in css
    assert "function toggleTheme" in js
    assert "data-theme" in js
    assert "settings-theme" in html


def test_new_renderers_have_no_storage_or_inline():
    js = _read("app.js")
    for name in ("renderAgentsView", "renderSecurityView", "renderSettingsView"):
        renderer = js.split(f"function {name}", 1)[1].split(
            "\n/* ----------", 1)[0]
        assert "api(" in renderer
        for forbidden in ("localStorage", "sessionStorage",
                          "document.cookie", "fetch(", "XMLHttpRequest",
                          "onclick="):
            assert forbidden not in renderer, (name, forbidden)
    html = _read("index.html")
    for view in A41_VIEWS:
        template = html.split(f'<template id="tpl-{view}">', 1)[1].split(
            "</template>", 1)[0]
        for forbidden in ("onclick=", "onload=", "<script", "style="):
            assert forbidden not in template, (view, forbidden)


def test_new_renderers_only_call_their_endpoints():
    js = _read("app.js")
    agents = js.split("function renderAgentsView", 1)[1].split(
        "function renderSecurityView", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', agents)
    assert calls == ["/api/v1/agents"], calls
    security = js.split("function renderSecurityView", 1)[1].split(
        "function currentTheme", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', security)
    assert calls == ["/api/v1/security"], calls
    settings = js.split("function renderSettingsView", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', settings)
    assert calls == ["/api/v1/sessions/me"], calls


def test_catalog_rows_reference_real_gates(tmp_path):
    plane = make_plane(tmp_path, start=False)
    catalog = plane.agent_catalog()
    by_name = {agent["name"]: agent for agent in catalog}
    # Coder writes are change-set gated; vision/voice are simulated and
    # labeled; nothing claims a capability it cannot back with a gate.
    assert "change set" in by_name["coder"]["gate"].lower() or \
        "filesystem" in by_name["coder"]["gate"].lower()
    assert by_name["forge-vision"]["simulated"] is True
    assert by_name["forge-voice"]["simulated"] is True
    assert by_name["forge-computer"]["simulated"] is True
