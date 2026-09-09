"""Deployment (A64): manifests, artifact builds, deploy, rollback.

Deployments package a project snapshot (bounded file walk, real
sha256 hashes) and stage it into a target directory outside the
source project. Every extraction verifies each file's hash against
the manifest before writing. Rollback restores the previous
artifact version. Nothing here talks to a network — "deployment"
means a verifiable local staging, honestly labeled.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

MAX_DEPLOYMENTS = 8
MAX_FILES = 1000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_VERSIONS = 3
SKIP_DIRS = {".git", ".forge", "__pycache__", ".venv", "node_modules",
             ".arena", "dist", "build", "deployments"}

_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentManager:
    """Validated project deployments persisted in the plane database."""

    def __init__(self, db: Any, project_id: str, project_root: str | Path,
                 artifacts_dir: str | Path) -> None:
        self._db = db
        self.project_id = project_id
        self.root = Path(project_root).resolve()
        self.artifacts_dir = Path(artifacts_dir)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS deployments (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                files_count INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT NOT NULL DEFAULT '',
                manifest_path TEXT NOT NULL DEFAULT '',
                build_index INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {"deployment_id": row["id"],
                "project_id": row["project_id"],
                "name": row["name"], "version": row["version"],
                "status": row["status"], "target": row["target"],
                "files_count": row["files_count"],
                "artifact_path": row["artifact_path"],
                "manifest_path": row["manifest_path"],
                "build_index": row["build_index"],
                "actor": row["actor"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def _get(self, deployment_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM deployments WHERE id = ? AND project_id = ?",
            (deployment_id, self.project_id))
        return self._row_to_dict(row) if row else None

    def create(self, name: str, version: str, actor: str
               ) -> dict[str, Any]:
        name = (name or "").strip().lower()
        version = (version or "").strip()
        if not _NAME.match(name):
            raise ValueError(
                "Deployment names must match [a-z][a-z0-9_-]{2,48}")
        if not _VERSION.match(version):
            raise ValueError(
                "Deployment versions must match [A-Za-z0-9._+-]{1,32}")
        count_row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM deployments WHERE project_id = ?",
            (self.project_id,))
        if count_row and int(count_row["n"]) >= MAX_DEPLOYMENTS:
            raise ValueError(f"deployment limit reached ({MAX_DEPLOYMENTS})")
        now = time.time()
        deployment_id = uuid.uuid4().hex[:12]
        self._db.execute(
            "INSERT INTO deployments (id, project_id, name, version, "
            "status, actor, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'created', ?, ?, ?)",
            (deployment_id, self.project_id, name, version, actor,
             now, now))
        return self._get(deployment_id) or {}

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM deployments WHERE project_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (self.project_id,))
        return [self._row_to_dict(row) for row in rows]

    def get(self, deployment_id: str) -> dict[str, Any]:
        record = self._get(deployment_id)
        if record is None:
            raise ValueError(f"Unknown deployment: {deployment_id}")
        return record

    def _collect(self) -> tuple[dict[str, str], list[Path]]:
        entries: dict[str, str] = {}
        files: list[Path] = []
        total = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            size = path.stat().st_size
            total += size
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValueError(
                    f"deployment bounds exceeded "
                    f"({MAX_FILES} files / "
                    f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB)")
            rel = path.relative_to(self.root).as_posix()
            entries[rel] = _file_hash(path)
            files.append(path)
        if not files:
            raise ValueError("project has no deployable files")
        return entries, files

    def build(self, deployment_id: str, actor: str) -> dict[str, Any]:
        record = self.get(deployment_id)
        if record["status"] not in ("created", "built", "deployed",
                                     "rolled_back"):
            raise ValueError(
                f"cannot build a deployment in status "
                f"{record['status']!r}")
        entries, _files = self._collect()
        existing_indices = [
            int(artifact.stem.rsplit("-", 1)[-1])
            for artifact in self.artifacts_dir.glob(
                f"{deployment_id}-*.zip")]
        build_index = max([record["build_index"],
                           *existing_indices]) + 1
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": "forge-deployment", "deployment_id": deployment_id,
            "project_id": self.project_id, "name": record["name"],
            "version": record["version"], "build_index": build_index,
            "files": entries, "created_at": time.time(),
        }
        manifest_path = self.artifacts_dir / (
            f"{deployment_id}-{build_index}.manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=1),
                                 encoding="utf-8")
        artifact_path = self.artifacts_dir / (
            f"{deployment_id}-{build_index}.zip")
        with zipfile.ZipFile(artifact_path, "w",
                             zipfile.ZIP_DEFLATED) as archive:
            for rel, _hash in sorted(entries.items()):
                archive.write(self.root / rel, arcname=rel)
        self._prune_versions(deployment_id)
        self._db.execute(
            "UPDATE deployments SET status = 'built', "
            "files_count = ?, artifact_path = ?, manifest_path = ?, "
            "build_index = ?, updated_at = ? WHERE id = ?",
            (len(entries), str(artifact_path), str(manifest_path),
             build_index, time.time(), deployment_id))
        return self.get(deployment_id)

    def _prune_versions(self, deployment_id: str) -> None:
        manifest_files = sorted(
            self.artifacts_dir.glob(f"{deployment_id}-*.manifest.json"))
        artifact_files = sorted(
            self.artifacts_dir.glob(f"{deployment_id}-*.zip"))
        for stale in manifest_files[:-MAX_VERSIONS]:
            stale.unlink(missing_ok=True)
        for stale in artifact_files[:-MAX_VERSIONS]:
            stale.unlink(missing_ok=True)

    def _extract_verified(self, record: dict[str, Any],
                          target: Path) -> None:
        manifest_path = Path(record["manifest_path"])
        artifact_path = Path(record["artifact_path"])
        if not manifest_path.is_file() or not artifact_path.is_file():
            raise ValueError("deployment artifact is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("files") or {}
        target.mkdir(parents=True, exist_ok=True)
        existing = {path.name for path in target.iterdir()} \
            if target.exists() else set()
        if existing and record["target"] == str(target):
            # Re-deploying the same deployment over its own target.
            pass
        elif existing:
            raise ValueError(
                "target directory is not empty and belongs to nothing "
                "known; refusing to overwrite")
        with zipfile.ZipFile(artifact_path, "r") as archive:
            for rel in archive.namelist():
                data = archive.read(rel)
                if _sha256(data) != expected.get(rel):
                    raise ValueError(
                        f"artifact hash mismatch for {rel!r}; refusing "
                        "to deploy")
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        # Every manifest file must have been present.
        missing = set(expected) - set(
            zipfile.ZipFile(artifact_path, "r").namelist())
        if missing:
            raise ValueError(
                f"artifact is missing {len(missing)} manifest files")

    def deploy(self, deployment_id: str, target: str, actor: str
               ) -> dict[str, Any]:
        record = self.get(deployment_id)
        if record["status"] not in ("built", "deployed", "rolled_back"):
            raise ValueError(
                f"cannot deploy a deployment in status "
                f"{record['status']!r}")
        target_path = Path(target).resolve()
        if target_path.is_relative_to(self.root):
            raise ValueError("deploy target must live outside the "
                             "source project")
        self._extract_verified(record, target_path)
        self._db.execute(
            "UPDATE deployments SET status = 'deployed', target = ?, "
            "updated_at = ? WHERE id = ?",
            (str(target_path), time.time(), deployment_id))
        return self.get(deployment_id)

    def rollback(self, deployment_id: str, actor: str
                 ) -> dict[str, Any]:
        record = self.get(deployment_id)
        if record["status"] != "deployed":
            raise ValueError(
                f"cannot roll back a deployment in status "
                f"{record['status']!r}; roll back only from deployed")
        if not record["target"]:
            raise ValueError("deployment has no recorded target")
        artifacts = sorted(self.artifacts_dir.glob(
            f"{deployment_id}-*.zip"))
        manifests = sorted(self.artifacts_dir.glob(
            f"{deployment_id}-*.manifest.json"))
        current = int(Path(record["artifact_path"]).stem
                      .rsplit("-", 1)[-1]) if record["artifact_path"] \
            else 0
        previous_index = current - 1
        previous_artifact = next(
            (artifact for artifact in artifacts
             if int(artifact.stem.rsplit("-", 1)[-1])
             == previous_index), None)
        previous_manifest = next(
            (manifest for manifest in manifests
             if int(manifest.stem[:-len(".manifest")]
                    .rsplit("-", 1)[-1]) == previous_index), None)
        if previous_artifact is None or previous_manifest is None:
            raise ValueError("no previous artifact version to restore")
        restored = {
            **record,
            "manifest_path": str(previous_manifest),
            "artifact_path": str(previous_artifact),
        }
        self._extract_verified(restored, Path(record["target"]))
        self._db.execute(
            "UPDATE deployments SET status = 'rolled_back', "
            "build_index = ?, manifest_path = ?, artifact_path = ?, "
            "updated_at = ? WHERE id = ?",
            (previous_index, str(previous_manifest),
             str(previous_artifact), time.time(), deployment_id))
        return self.get(deployment_id)
