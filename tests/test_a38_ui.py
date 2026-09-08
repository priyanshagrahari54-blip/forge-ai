"""Browser cockpit orchestrations view contract tests (A38)."""
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

ORCH_HOOKS = """orch-refresh orch-form orch-requirement orch-chain
orch-list orch-detail""".split()

ORCH_ENDPOINTS = (
    "/api/v1/orchestrations", "/api/v1/orchestrations/approvals",
    "/api/v1/orchestrations/cancel",
)


def _read(name: str) -> str:
    return (WEB / name).read_text()


def allow_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="agents-ok", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="writes-ok", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def test_orchestrations_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-orchestrations">' in html
    for hook in ORCH_HOOKS:
        assert f'id="{hook}"' in html, hook
    assert 'href="#/orchestrations"' in html


def test_orchestrations_route_registered_with_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "orchestrations" in routes_block
    assert "renderOrchestrations" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Orchestrations" in palette
    assert "#/orchestrations" in palette


def test_orchestrations_renderer_only_talks_to_orchestration_endpoints():
    js = _read("app.js")
    renderer = js.split("function renderOrchestrations", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call.startswith("/api/v1/orchestrations"), call
    assert "/api/v1/orchestrations" in calls


def test_orchestrations_renderer_has_no_storage_or_credentials():
    js = _read("app.js")
    renderer = js.split("function renderOrchestrations", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-orchestrations">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def test_orchestration_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=allow_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the "
                                      "repository", "chain": False})
        assert submitted.status_code == 200
        listed = client.get("/api/v1/orchestrations", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["orchestrations"]
