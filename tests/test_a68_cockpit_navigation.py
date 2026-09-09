"""Cockpit navigation (A68): canonical keyboard shortcuts."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.cockpit_palette import VIEWS  # noqa: E402
from forge.cockpit_shortcuts import (  # noqa: E402
    CHORDS,
    KEYS,
    shortcut_entries,
    shortcut_payload,
    validate_shortcuts,
)
from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_shortcut_catalog_shape_and_uniqueness():
    entries = shortcut_entries()
    chords = [entry for entry in entries if entry["kind"] == "chord"]
    keys = [entry for entry in entries if entry["kind"] == "key"]
    assert len(chords) == 8
    assert any(entry["keys"] == "?" for entry in keys)
    assert any(entry["keys"] == "ctrl+k" for entry in keys)
    bindings = [entry["keys"] for entry in entries]
    assert len(bindings) == len(set(bindings))
    payload = shortcut_payload()
    assert payload["count"] == len(entries)
    assert "never execute anything" in payload["note"]


def test_chord_targets_are_real_views():
    view_targets = {view["target"] for view in VIEWS}
    for chord in CHORDS:
        assert chord["target"] in view_targets
    validate_shortcuts(shortcut_entries())  # catalog passes its own audit


def test_shortcut_validation_refuses_garbage():
    with pytest.raises(Exception):
        validate_shortcuts([{"keys": "not valid!", "kind": "chord",
                             "target": "dashboard"}])
    with pytest.raises(Exception):
        validate_shortcuts([{"keys": "g x", "kind": "chord",
                             "target": "no-such-view"}])
    with pytest.raises(Exception):
        validate_shortcuts([{"keys": "g d", "kind": "chord",
                             "target": "dashboard"},
                            {"keys": "g d", "kind": "chord",
                             "target": "tasks"}])


def test_cockpit_wires_shortcuts():
    web = Path(__file__).parent.parent / "forge" / "cockpit" / "web"
    js = (web / "app.js").read_text(encoding="utf-8")
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "styles.css").read_text(encoding="utf-8")
    assert 'api("/api/v1/commands/shortcuts")' in js
    assert "SHORTCUT_CHORDS" in js
    assert "initShortcuts" in js
    assert 'id="shortcuts"' in html
    assert 'id="shortcuts-open"' in html
    assert ".shortcut-row" in css
    # The cockpit stays class-toggling only (no inline styles).
    assert ".style" not in js


def test_shortcuts_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.get("/api/v1/commands/shortcuts", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["entries"])
        for entry in body["entries"]:
            assert "keys" in entry and "label" in entry
        anonymous = client.get("/api/v1/commands/shortcuts")
        assert anonymous.status_code in (401, 403)
