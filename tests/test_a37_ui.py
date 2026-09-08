"""Browser cockpit memory view contract tests (A37)."""
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

MEMORY_HOOKS = """memory-refresh memory-form memory-kind memory-content
memory-entries memory-project-form memory-key memory-project-content
memory-project memory-approvals""".split()

MEMORY_ENDPOINTS = ("/api/v1/memory", "/api/v1/memory/approvals",
                    "/api/v1/memory/project/save",
                    "/api/v1/memory/delete")


def _read(name: str) -> str:
    return (WEB / name).read_text()


def memory_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="", effect="ALLOW"),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect="ALLOW"),
        PermissionRule(id="m-delete", resource=Resource.MEMORY,
                       operation="delete", scope="", effect="ALLOW"),
    ])


def test_memory_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-memory">' in html
    for hook in MEMORY_HOOKS:
        assert f'id="{hook}"' in html, hook
    assert 'href="#/memory"' in html


def test_memory_route_registered_with_template_and_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "memory" in routes_block
    assert "renderMemory" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Memory" in palette
    assert "#/memory" in palette


def test_memory_renderer_only_talks_to_memory_endpoints():
    js = _read("app.js")
    renderer = js.split("async function renderMemory", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call in MEMORY_ENDPOINTS or call.startswith(
            "/api/v1/memory/"), call
    assert "/api/v1/memory" in calls


def test_memory_renderer_has_no_storage_or_credentials():
    js = _read("app.js")
    renderer = js.split("async function renderMemory", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-memory">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def test_memory_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=False, policy=memory_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        overview = client.get("/api/v1/memory", headers=headers)
        assert overview.status_code == 200
        assert overview.json() == {"session_entries": [],
                                   "project_keys": []}
        saved = client.post("/api/v1/memory", headers=headers, json={
            "kind": "note", "content": "ui test entry"})
        assert saved.status_code == 200
        assert saved.json()["allowed"] is True
        approvals = client.get("/api/v1/memory/approvals",
                               headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()
        project = client.get("/api/v1/memory/project", headers=headers)
        assert project.status_code == 200
        assert "project_keys" in project.json()
