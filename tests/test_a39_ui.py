"""Browser cockpit vision view contract tests (A39)."""
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

VISION_HOOKS = """vision-file vision-analyze vision-propose vision-result
vision-proposals vision-approvals""".split()


def _read(name: str) -> str:
    return (WEB / name).read_text()


def vision_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW"),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect="ALLOW"),
    ])


def test_vision_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-vision">' in html
    for hook in VISION_HOOKS:
        assert f'id="{hook}"' in html, hook
    assert 'href="#/vision"' in html


def test_vision_route_registered_with_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "vision" in routes_block
    assert "renderVision" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Vision" in palette
    assert "#/vision" in palette


def test_vision_renderer_only_talks_to_vision_endpoints():
    js = _read("app.js")
    renderer = js.split("function renderVision", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call.startswith("/api/v1/vision"), call
    assert "/api/v1/vision/analyze" in calls
    assert "/api/v1/vision/approvals" in calls


def test_vision_renderer_has_no_storage():
    js = _read("app.js")
    renderer = js.split("function renderVision", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-vision">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def test_vision_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=vision_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        caps = client.get("/api/v1/vision/capabilities", headers=headers)
        assert caps.status_code == 200
        analyzed = client.post("/api/v1/vision/analyze", headers=headers,
                               json={"image_b64": b64(make_png())})
        assert analyzed.status_code == 200
        assert analyzed.json()["simulation"] is True
        proposed = client.post("/api/v1/vision/propose", headers=headers,
                               json={"image_b64": b64(make_png())})
        assert proposed.status_code == 200
        assert proposed.json()["proposals"]
        approvals = client.get("/api/v1/vision/approvals", headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()
