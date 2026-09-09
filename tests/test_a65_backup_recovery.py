"""Backup & recovery (A65): verified snapshots, drift, restore."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_backup_create_captures_db_and_files(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.backup_create(session, "nightly")
        assert created["status"] == "created"
        assert created["files_count"] >= 3
        assert created["size_bytes"] > 0
        archive = tmp_path / "backups" / f"{created['backup_id']}.zip"
        assert archive.is_file()
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
            assert "cockpit.db" in names
            assert "manifest.json" in names
            assert "projects/demo/app.py" in names
        with pytest.raises(Exception):
            plane.backup_create(session, "bad label!")


def test_backup_verify_clean_and_detects_drift(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.backup_create(session, "verify-me")
        verified = plane.backup_verify(session, created["backup_id"])
        assert verified["db_ok"] is True
        assert verified["files_ok"] == verified["files_total"]
        assert verified["drift"] == []
        assert "out of scope" in verified["note"]
        # Mutate and delete files; drift must be detected honestly.
        (root / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
        (root / "tests" / "test_app.py").unlink()
        drift_report = plane.backup_verify(session, created["backup_id"])
        assert drift_report["db_ok"] is True  # state db untouched
        states = {entry["file"]: entry["state"]
                  for entry in drift_report["drift"]}
        assert states.get("app.py") == "changed"
        assert states.get("tests/test_app.py") == "missing"


def test_backup_cap_prunes_oldest(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        for index in range(5):
            plane.backup_create(session, f"backup-{index}")
        sixth = plane.backup_create(session, "backup-6")
        backups = plane.backup_list(session)["backups"]
        assert len(backups) == 5
        ids = {entry["backup_id"] for entry in backups}
        assert sixth["backup_id"] in ids
        assert all(not (tmp_path / "backups" / f"{entry['backup_id']}.zip")
                   .is_file() or entry["backup_id"] in ids
                   for entry in backups)


def test_restore_requires_stopped_plane_and_restores(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.record_failure(session, "system", "pre-restore failure")
        created = plane.backup_create(session, "restore-me")
        with pytest.raises(Exception):
            plane.backup_restore(session, created["backup_id"])
        db_path = Path(plane.config.db_path)
        archive = tmp_path / "backups" / f"{created['backup_id']}.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            backup_bytes = zf.read("cockpit.db")
        # Mutate the database state after the backup.
        plane.record_failure(session, "system", "post-restore failure")
        plane.stop(wait=True)
        restored = plane.backup_restore(session, created["backup_id"])
        assert restored["restored"] is True
        # The live database file is the verified snapshot again.
        assert db_path.read_bytes() == backup_bytes
        # The restored database has the pre-restore state only.
        plane2 = make_plane(tmp_path, start=True)
        client2 = make_client(plane2)
        with client2:
            payload2, _token2, _headers2 = login(client2)
            session2 = plane2.sessions.get(payload2["session_id"])
            lessons = plane2.failure_lessons(session2)["lessons"]
            assert any("pre-restore failure" in entry["fingerprint"]
                       for entry in lessons)
            assert not any("post-restore failure" in entry["fingerprint"]
                           for entry in lessons)


def test_backup_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/backups", headers=headers,
                              json={"label": "api-backup"})
        assert created.status_code == 200
        backup_id = created.json()["backup_id"]
        verified = client.post(f"/api/v1/backups/{backup_id}/verify",
                               headers=headers)
        assert verified.status_code == 200
        assert verified.json()["db_ok"] is True
        listed = client.get("/api/v1/backups", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["backups"][0]["backup_id"] == backup_id
        got = client.get(f"/api/v1/backups/{backup_id}", headers=headers)
        assert got.status_code == 200
        bad = client.post("/api/v1/backups", headers=headers,
                          json={"label": ""})
        assert bad.status_code == 400
