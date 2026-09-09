"""Command palette (A67): server-canonical palette for the cockpit."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.cockpit_palette import (  # noqa: E402
    VIEWS,
    palette_entries,
    palette_payload,
    validate_entries,
)
from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_palette_covers_cockpit_views():
    entries = palette_entries()
    views = [entry for entry in entries if entry["kind"] == "view"]
    actions = [entry for entry in entries if entry["kind"] == "action"]
    assert len(views) >= 20
    labels = {entry["label"] for entry in views}
    assert "Go to Overview" in labels
    assert "Go to System" in labels
    assert any(entry["target"] == "tasks" for entry in actions)
    payload = palette_payload()
    assert payload["count"] == len(entries)
    assert "no execution power" in payload["note"]


def test_server_targets_exist_in_client_routes():
    app_js = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "app.js"
    source = app_js.read_text(encoding="utf-8")
    match = re.search(r"const ROUTES = \{(.*?)\n\};", source, re.DOTALL)
    assert match, "ROUTES block not found in app.js"
    route_keys = set(re.findall(r"^\s{2}(\w+): \{", match.group(1),
                                re.MULTILINE))
    assert "dashboard" in route_keys
    for entry in palette_entries():
        if entry["kind"] == "view":
            assert entry["target"] in route_keys, \
                f"palette target {entry['target']!r} missing from ROUTES"


def test_client_fetches_the_server_palette():
    app_js = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "app.js"
    source = app_js.read_text(encoding="utf-8")
    assert "api(\"/api/v1/commands/palette\")" in source
    assert "syncServerPalette" in source
    # Navigation targets stay hash routes in the client merge.
    assert 'window.location.hash = "#/" + target' in source
    # The palette markup and keyboard binding exist.
    index = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "index.html"
    html = index.read_text(encoding="utf-8")
    assert 'id="palette"' in html
    assert 'id="palette-input"' in html
    assert "palette-open" in html


def test_palette_validation_defense():
    validate_entries(palette_entries())  # valid catalog passes
    with pytest.raises(Exception):
        validate_entries([{"label": "x", "kind": "view",
                           "target": "not a route!"}])
    with pytest.raises(Exception):
        validate_entries([{"label": "x", "kind": "daemon", "target": "ok"}])


def test_palette_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.get("/api/v1/commands/palette", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["entries"])
        assert all("label" in entry and "kind" in entry
                   for entry in body["entries"])
        # No session: read requires authentication.
        anonymous = client.get("/api/v1/commands/palette")
        assert anonymous.status_code in (401, 403)
