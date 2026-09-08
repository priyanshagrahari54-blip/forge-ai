"""Browser cockpit computer-use view contract tests (A40)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402
from helpers_a39 import b64, make_png  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

COMPUTER_HOOKS = """computer-file computer-goal computer-observe
computer-propose computer-cycle computer-tree computer-proposals
computer-action computer-target computer-reason computer-act
computer-history computer-approvals""".split()


def _read(name: str) -> str:
    return (WEB / name).read_text()


def computer_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW"),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="d-move", resource=Resource.DESKTOP,
                       operation="mouse_move", scope="", effect="ALLOW"),
    ])


def test_computer_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-computer">' in html
    for hook in COMPUTER_HOOKS:
        assert f'id="{hook}"' in html, hook
    assert 'href="#/computer"' in html


def test_computer_route_registered_with_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "computer" in routes_block
    assert "renderComputer" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Computer Use" in palette
    assert "#/computer" in palette


def test_computer_renderer_only_talks_to_computer_endpoints():
    js = _read("app.js")
    renderer = js.split("function renderComputer", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call.startswith("/api/v1/computer"), call
    assert "/api/v1/computer/observe" in calls
    assert "/api/v1/computer/act" in calls
    assert "/api/v1/computer/approvals" in calls


def test_computer_renderer_has_no_storage():
    js = _read("app.js")
    renderer = js.split("function renderComputer", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-computer">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def test_computer_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=computer_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        observed = client.post("/api/v1/computer/observe", headers=headers,
                               json={"image_b64": b64(make_png()),
                                     "goal": "click OK"})
        assert observed.status_code == 200
        proposed = client.post("/api/v1/computer/propose", headers=headers,
                               json={"image_b64": b64(make_png()),
                                     "goal": "click OK"})
        assert proposed.status_code == 200
        history = client.get("/api/v1/computer/history", headers=headers)
        assert history.status_code == 200
        approvals = client.get("/api/v1/computer/approvals", headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()
