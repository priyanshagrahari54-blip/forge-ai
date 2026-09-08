"""Browser cockpit desktop view contract tests (A35).

The Desktop view adds a controlled-execution surface to the cockpit. These
tests pin the contracts that must not drift: honest capability rendering
(simulation is labeled as simulation), every capability backed by the A35
pipeline, the approval-token flow that carries the single-use token from
approve into the resubmitted action, repo-standard table markup, and live
endpoints behind the renderer.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import PermissionPolicy, PermissionRule, Resource  # noqa: E402

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

DESK_HOOKS = """desk-refresh desk-profile desk-provider desk-state
desk-capabilities desk-form desk-action desk-target desk-params
desk-reason desk-result desk-approvals""".split()

DESKTOP_ENDPOINTS = ("/api/v1/desktop/capabilities",
                     "/api/v1/desktop/state",
                     "/api/v1/desktop/approvals",
                     "/api/v1/desktop/act")


def _read(name: str) -> str:
    return (WEB / name).read_text()


def desktop_policy():
    return PermissionPolicy(rules=[PermissionRule(
        id=f"r{i}", resource=Resource.DESKTOP, operation=op,
        effect="ALLOW") for i, op in enumerate([
            "screenshot", "read_screen", "window", "process",
            "system_info", "file_access", "clipboard", "launch",
            "mouse_move", "mouse_click", "keyboard"])])


def test_desktop_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-desktop">' in html
    for hook in DESK_HOOKS:
        assert f'id="{hook}"' in html, hook


def test_desktop_route_registered_with_template_and_renderer():
    html = _read("index.html")
    js = _read("app.js")
    assert "tpl-desktop" in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "desktop" in routes_block
    assert "renderDesktop" in js
    # Navigation only, via the palette.
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Desktop" in palette
    assert "#/desktop" in palette


def test_desktop_renderer_only_talks_to_desktop_endpoints():
    js = _read("app.js")
    renderer = js.split("async function renderDesktop", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    allowed = DESKTOP_ENDPOINTS + tuple(
        f"/api/v1/desktop/approvals/{suffix}"
        for suffix in ("approve", "deny"))
    for call in calls:
        assert call in DESKTOP_ENDPOINTS or call.startswith(
            "/api/v1/desktop/approvals/"), call
    assert calls[0] == "/api/v1/desktop/capabilities"


def test_desktop_approve_flow_carries_single_use_token():
    js = _read("app.js")
    # Approve must capture the minted token id...
    approve = js.split("approve.addEventListener", 1)[1].split(
        "deny.addEventListener", 1)[0]
    assert "decision.token_id" in approve
    assert "state.deskToken" in approve
    # ...the resubmitted action must carry it as approval_id...
    assert "body.approval_id" in js
    # ...and a successful execution consumes it (single use).
    assert "if (result.executed) state.deskToken = null" in js
    assert "deskToken: null" in js


def test_desktop_capability_matrix_uses_data_table_markup():
    js = _read("app.js")
    renderer = js.split("async function renderDesktop", 1)[1]
    # Repo CSS only styles table.data-table; div-based rows are rejected.
    assert 'el("table", "data-table")' in renderer
    assert "div.table-row" not in js
    html = _read("index.html")
    assert 'class="form-grid"' in html


def test_desktop_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=False, policy=desktop_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        capabilities = client.get("/api/v1/desktop/capabilities",
                                  headers=headers)
        assert capabilities.status_code == 200
        body = capabilities.json()
        # The cockpit must never misrepresent a simulation as real control.
        assert body["status"] == "simulation"
        assert "fake" in body["provider"].lower()
        assert body["capabilities"]
        state = client.get("/api/v1/desktop/state", headers=headers)
        assert state.status_code == 200
        for key in ("profile", "observations", "provider"):
            assert key in state.json(), key
        approvals = client.get("/api/v1/desktop/approvals",
                               headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()


def test_desktop_view_has_no_credentials_or_storage_authority():
    js = _read("app.js")
    renderer = js.split("async function renderDesktop", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest"):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-desktop">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden
