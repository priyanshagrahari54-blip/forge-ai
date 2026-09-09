"""Deployment (A64): validated manifests, builds, deploy, rollback."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def make_deployed(plane, session, name="webapp", version="1.0.0",
                  target=None):
    target = target or str(Path(plane.projects["demo"].root).parent /
                           "deploy-target" / name)
    created = plane.deployment_create(session, name, version)
    built = plane.deployment_build(session, created["deployment_id"])
    deployed = plane.deployment_deploy(session, created["deployment_id"],
                                       target)
    return created, built, deployed


def test_create_validation_and_caps(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.deployment_create(session, "Bad Name!", "1.0.0")
        with pytest.raises(Exception):
            plane.deployment_create(session, "webapp", "")
        created = plane.deployment_create(session, "webapp", "1.0.0")
        assert created["status"] == "created"
        assert created["deployment_id"]
        with pytest.raises(Exception):
            plane.deployment_get(session, "does-not-exist")
        for index in range(7):
            plane.deployment_create(session, f"app-{index}", "1.0.0")
        with pytest.raises(Exception):
            plane.deployment_create(session, "one-too-many", "1.0.0")


def test_build_produces_real_verified_artifact(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").write_text("def health(): return True\n",
                                 encoding="utf-8")
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.deployment_create(session, "webapp", "1.0.0")
        built = plane.deployment_build(session, created["deployment_id"])
        assert built["status"] == "built"
        assert built["files_count"] >= 1
        artifact = Path(built["artifact_path"])
        manifest = Path(built["manifest_path"])
        assert artifact.is_file() and manifest.is_file()
        manifest_data = json.loads(
            manifest.read_text(encoding="utf-8"))
        assert manifest_data["format"] == "forge-deployment"
        assert "app.py" in manifest_data["files"]
        with zipfile.ZipFile(artifact, "r") as archive:
            content = archive.read("app.py").decode("utf-8")
        assert "def health" in content


def test_deploy_extracts_verified_files_outside_project(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").write_text("def health(): return True\n",
                                 encoding="utf-8")
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        target = str(root.parent / "stage" / "webapp")
        created, _built, deployed = make_deployed(
            plane, session, target=target)
        assert deployed["status"] == "deployed"
        assert deployed["target"] == str(Path(target).resolve())
        staged = Path(deployed["target"]) / "app.py"
        assert staged.is_file()
        assert "def health" in staged.read_text(encoding="utf-8")
        # Refuse targets inside the project.
        with pytest.raises(Exception):
            plane.deployment_deploy(session, created["deployment_id"],
                                    str(root / "out"))
        # Refuse unknown non-empty targets.
        foreign = root.parent / "foreign"
        foreign.mkdir(exist_ok=True)
        (foreign / "keep.txt").write_text("do not touch", encoding="utf-8")
        with pytest.raises(Exception):
            plane.deployment_deploy(session, created["deployment_id"],
                                    str(foreign))
        assert (foreign / "keep.txt").read_text(
            encoding="utf-8") == "do not touch"


def test_rollback_restores_previous_version(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    app = root / "app.py"
    app.write_text("VERSION = 1\n", encoding="utf-8")
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.deployment_create(session, "webapp", "1.0.0")
        target = str(root.parent / "stage" / "webapp")
        plane.deployment_build(session, created["deployment_id"])
        plane.deployment_deploy(session, created["deployment_id"], target)
        staged = Path(target) / "app.py"
        assert "VERSION = 1" in staged.read_text(encoding="utf-8")
        app.write_text("VERSION = 2\n", encoding="utf-8")
        plane.deployment_build(session, created["deployment_id"])
        plane.deployment_deploy(session, created["deployment_id"], target)
        assert "VERSION = 2" in staged.read_text(encoding="utf-8")
        rolled = plane.deployment_rollback(
            session, created["deployment_id"])
        assert rolled["status"] == "rolled_back"
        assert "VERSION = 1" in staged.read_text(encoding="utf-8")
        # No previous version left beyond the last three builds.
        with pytest.raises(Exception):
            plane.deployment_rollback(session, created["deployment_id"])


def test_deployment_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/deployments", headers=headers,
                              json={"name": "webapp", "version": "1.0.0"})
        assert created.status_code == 200
        deployment_id = created.json()["deployment_id"]
        built = client.post(f"/api/v1/deployments/{deployment_id}/build",
                            headers=headers)
        assert built.status_code == 200
        target = str(root.parent / "stage" / "api")
        deployed = client.post(
            f"/api/v1/deployments/{deployment_id}/deploy",
            headers=headers, json={"target": target})
        assert deployed.status_code == 200
        assert (Path(target) / "app.py").is_file()
        listed = client.get("/api/v1/deployments", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["deployments"][0]["deployment_id"] == \
            deployment_id
        got = client.get(f"/api/v1/deployments/{deployment_id}",
                         headers=headers)
        assert got.status_code == 200
        bad = client.post("/api/v1/deployments", headers=headers,
                          json={"name": "Bad Name!", "version": "1.0.0"})
        assert bad.status_code == 400
