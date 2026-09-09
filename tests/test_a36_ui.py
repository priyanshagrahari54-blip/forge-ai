"""Browser cockpit voice view contract tests (A36).

The Voice view adds the spoken loop to the cockpit. These tests pin the
contracts that must not drift: template hooks, route/template/renderer
wiring, navigation-only palette, same-origin API calls, CSP media
handling for audio playback, no storage/credentials, and live endpoints
behind the renderer.
"""
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

VOICE_HOOKS = """voice-refresh voice-capabilities voice-form voice-text
voice-synth-form voice-synth-text voice-audio voice-result
voice-approvals""".split()

VOICE_ENDPOINTS = ("/api/v1/voice/capabilities",
                   "/api/v1/voice/approvals",
                   "/api/v1/voice/process",
                   "/api/v1/voice/synthesize")


def _read(name: str) -> str:
    return (WEB / name).read_text()


def voice_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="voice-status", resource=Resource.VOICE,
                       operation="command", scope="status", effect="ALLOW"),
    ])


def test_voice_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-voice">' in html
    for hook in VOICE_HOOKS:
        assert f'id="{hook}"' in html, hook
    # Nav link exists and is real.
    assert 'href="#/voice"' in html


def test_voice_route_registered_with_template_and_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "voice" in routes_block
    assert "renderVoice" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Voice" in palette
    assert "#/voice" in palette


def test_voice_renderer_only_talks_to_voice_endpoints():
    js = _read("app.js")
    renderer = js.split("async function renderVoice", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call in VOICE_ENDPOINTS or call.startswith(
            "/api/v1/voice/approvals/"), call


def test_voice_renderer_plays_audio_and_labels_simulation():
    js = _read("app.js")
    section = js.split("/* ---------- voice (A36) ---------- */", 1)[1].split(
        "/* ---------- command palette ---------- */", 1)[0]
    # Playback uses blob URLs (CSP must allow media-src blob:).
    assert "URL.createObjectURL" in section
    assert 'type: "audio/wav"' in section
    assert "atob(" in section
    # Simulation is labeled, never disguised.
    assert "simulation" in section
    assert "voiceToken" in section  # approval token carried into resubmit


def test_voice_view_has_no_storage_credentials_or_inline_handlers():
    js = _read("app.js")
    renderer = js.split("async function renderVoice", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-voice">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def test_csp_allows_blob_media_only(tmp_path):
    plane = make_plane(tmp_path, start=False, policy=voice_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        response = client.get("/")
        csp = response.headers["Content-Security-Policy"]
        assert "media-src 'self' blob:" in csp
        # Script/style stay locked to self.
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp


def test_voice_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=False, policy=voice_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        capabilities = client.get("/api/v1/voice/capabilities",
                                  headers=headers)
        assert capabilities.status_code == 200
        body = capabilities.json()
        assert body["status"] == "simulation"
        assert body["transcriber"]["name"]
        approvals = client.get("/api/v1/voice/approvals", headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()
        process = client.post("/api/v1/voice/process", headers=headers,
                              json={"text": "check status"})
        assert process.status_code == 200
        assert process.json()["ok"] is True
        synthesize = client.post("/api/v1/voice/synthesize", headers=headers,
                                 json={"text": "check status"})
        assert synthesize.status_code == 200
        assert synthesize.json()["audio_b64"]
