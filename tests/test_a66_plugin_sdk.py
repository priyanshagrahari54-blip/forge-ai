"""Plugin SDK (A66): validated declarative plugins, honest bindings."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def valid_manifest(**overrides) -> dict:
    manifest = {
        "format": "forge-plugin-manifest", "format_version": 1,
        "name": "csv-exporter", "version": "1.0.0",
        "kind": "executor", "description": "exports csv files",
        "capabilities": ["coding"], "entrypoint": "plugins.csv.export",
    }
    manifest.update(overrides)
    return manifest


def test_manifest_validation_is_strict(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        bad_cases = [
            {"name": "x"},  # not a manifest
            valid_manifest(format="other"),
            valid_manifest(format_version=99),
            valid_manifest(name="Bad Name!"),
            valid_manifest(version="v1"),
            valid_manifest(kind="daemon"),
            valid_manifest(capabilities=[]),
            valid_manifest(capabilities=["mind-reading"]),
            valid_manifest(capabilities=["coding"] * 17),
            valid_manifest(entrypoint="import os; os.system('x')"),
        ]
        for case in bad_cases:
            with pytest.raises(Exception):
                plane.plugin_install(session, case)
        assert plane.plugin_list(session)["plugins"] == []


def test_install_lists_and_removes(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        installed = plane.plugin_install(session, valid_manifest())
        assert installed["plugin_id"]
        assert installed["name"] == "csv-exporter"
        listed = plane.plugin_list(session)["plugins"]
        assert [entry["plugin_id"] for entry in listed] == \
            [installed["plugin_id"]]
        removed = plane.plugin_remove(session, installed["plugin_id"])
        assert removed["name"] == "csv-exporter"
        assert plane.plugin_list(session)["plugins"] == []
        with pytest.raises(Exception):
            plane.plugin_remove(session, installed["plugin_id"])


def test_binding_reports_real_capabilities_honestly(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        # coding is backed by the real built-in coder executor; the
        # rest are declared only (canonical but unregistered here).
        installed = plane.plugin_install(session, valid_manifest(
            capabilities=["coding", "documentation", "browser"]))
        status = plane.plugin_status(session, installed["plugin_id"])
        assert status["declared"] == ["coding", "documentation",
                                      "browser"]
        assert "coding" in status["real"]
        assert status["unbound"] == ["documentation", "browser"]
        assert status["honest"] is False
        # Declarations alone never add to the real catalog.
        real = plane._real_capabilities()
        assert "documentation" not in real


def test_plugin_install_is_session_isolated_and_capped(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        for index in range(16):
            plane.plugin_install(
                session,
                valid_manifest(name=f"plugin-{index}"))
        with pytest.raises(Exception):
            plane.plugin_install(session, valid_manifest(name="one-more"))
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        assert plane.plugin_list(bob)["plugins"] == []


def test_plugin_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        installed = client.post("/api/v1/plugins", headers=headers,
                                json={"manifest": valid_manifest()})
        assert installed.status_code == 200
        plugin_id = installed.json()["plugin_id"]
        status = client.get(f"/api/v1/plugins/{plugin_id}", headers=headers)
        assert status.status_code == 200
        assert "coding" in status.json()["real"]
        listed = client.get("/api/v1/plugins", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["plugins"][0]["plugin_id"] == plugin_id
        bad = client.post("/api/v1/plugins", headers=headers,
                          json={"manifest": {"name": "nope"}})
        assert bad.status_code == 400
        removed = client.delete(f"/api/v1/plugins/{plugin_id}",
                                headers=headers)
        assert removed.status_code == 200
