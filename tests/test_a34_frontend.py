"""Frontend security and serving tests (A34).

The browser bundle must contain no credentials, no provider calls, and no
ambient authority — and the backend must reject anything the UI hides.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"


def _sources() -> dict[str, str]:
    return {
        "index.html": (WEB / "index.html").read_text(),
        "app.js": (WEB / "app.js").read_text(),
        "styles.css": (WEB / "styles.css").read_text(),
    }


def test_frontend_served_with_safe_headers(tmp_path):
    plane = make_plane(tmp_path)
    client = make_client(plane)
    with client:
        index = client.get("/")
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        assert index.headers.get("Cache-Control") == "no-store"
        assert "Content-Security-Policy" in index.headers
        assert "nosniff" in index.headers.get(
            "X-Content-Type-Options", "")
        script = client.get("/app.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        styles = client.get("/styles.css")
        assert styles.status_code == 200


def test_frontend_contains_no_credentials_or_endpoints():
    sources = _sources()
    blob = "\n".join(sources.values())
    forbidden = [
        r"api[_-]?key", r"apikey", r"secret\s*[:=]", r"passwd",
        r"OPENAI", r"ANTHROPIC", r"sk-[A-Za-z0-9]{20,}",
        r"BEGIN .*PRIVATE KEY",
        r"https?://", r"localhost", r"127\.0\.0\.1", r"\beval\s*\(",
        r"document\.write", r"localStorage", r"sessionStorage",
        r"ollama", r"new\s+WebSocket",
    ]
    lowered = blob.lower()
    for pattern in forbidden:
        assert re.search(pattern, lowered, re.IGNORECASE) is None, pattern
    # Exactly one fetch helper; every call site targets same-origin /api.
    assert blob.count("fetch(") == 1
    assert "/api/v1/sessions" in blob
    assert "/api/v1/tasks" in blob
    # innerHTML is used only to clear containers, never with data.
    for match in re.finditer(r"\.innerHTML\s*=\s*(.+?);", blob):
        assert match.group(1).strip() == '""', match.group(0)


def test_frontend_sends_csrf_header_on_mutations():
    app_js = (WEB / "app.js").read_text()
    assert "X-Requested-With" in app_js
    assert "forge-cockpit" in app_js


def test_backend_rejects_hidden_ui_actions(tmp_path):
    """Hiding a button is not authorization: the API decides."""
    plane = make_plane(tmp_path)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] in ("SUCCEEDED", "FAILED")
        # The UI hides Pause for terminal tasks; manual API calls fail too.
        paused = client.post(f"/api/v1/tasks/{task_id}/pause",
                             headers=headers, json={})
        assert paused.status_code == 409
        # Forged approval ids fail closed.
        forged = client.post("/api/v1/approvals/doesnotexist1/approve",
                             headers=headers)
        assert forged.status_code == 404
