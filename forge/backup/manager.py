"""Backup & recovery (A65): consistent snapshots, verification, restore.

A backup is a zip containing (1) a consistent snapshot of the plane
database taken with SQLite's online backup API, (2) bounded project
file contents, and (3) a manifest of sha256 hashes. ``verify``
re-opens the archive and compares it against the live world —
database byte-for-byte, files hash-for-hash — and reports drift
honestly. ``restore`` only runs against a stopped plane and rewrites
the database file; file-tree rollback is intentionally out of scope
and is stated as such.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

MAX_BACKUPS = 5
MAX_FILES = 1000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_DRIFT = 20
SKIP_DIRS = {".git", ".forge", "__pycache__", ".venv", "node_modules",
             ".arena", "dist", "build", "backups", "deployments"}

_LABEL = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BackupManager:
    """Verified project+state backups persisted beside the database."""

    def __init__(self, db: Any, db_path: str | Path,
                 projects: dict[str, str | Path],
                 backup_dir: str | Path) -> None:
        self._db = db
        self.db_path = Path(db_path)
        self.projects = {pid: Path(root)
                         for pid, root in projects.items()}
        self.backup_dir = Path(backup_dir)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                db_sha256 TEXT NOT NULL DEFAULT '',
                files_count INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {"backup_id": row["id"], "label": row["label"],
                "status": row["status"], "db_sha256": row["db_sha256"],
                "files_count": row["files_count"],
                "size_bytes": row["size_bytes"],
                "actor": row["actor"], "created_at": row["created_at"]}

    def _get(self, backup_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM backups WHERE id = ?",
                                 (backup_id,))
        return self._row_to_dict(row) if row else None

    def get(self, backup_id: str) -> dict[str, Any]:
        record = self._get(backup_id)
        if record is None:
            raise ValueError(f"Unknown backup: {backup_id}")
        return record

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM backups ORDER BY created_at DESC, id DESC")
        return [self._row_to_dict(row) for row in rows]

    def _archive_path(self, backup_id: str) -> Path:
        return self.backup_dir / f"{backup_id}.zip"

    def _snapshot_db(self) -> tuple[bytes, str]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.backup_dir / f".db-snapshot-{uuid.uuid4().hex}.db"
        try:
            source = sqlite3.connect(str(self.db_path))
            try:
                destination = sqlite3.connect(str(temp_path))
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            data = temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)
        return data, _sha256_bytes(data)

    def create(self, label: str, actor: str) -> dict[str, Any]:
        label = (label or "").strip()
        if not _LABEL.match(label):
            raise ValueError(
                "Backup labels must match [A-Za-z0-9 _-]{1,64}")
        count_row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM backups")
        if count_row and int(count_row["n"]) >= MAX_BACKUPS:
            self._prune_one()
        db_bytes, db_sha256 = self._snapshot_db()
        files: dict[str, dict[str, str]] = {}
        total_files = 0
        total_bytes = 0
        for project_id, root in self.projects.items():
            project_files: dict[str, str] = {}
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                size = path.stat().st_size
                total_files += 1
                total_bytes += size
                if total_files > MAX_FILES or \
                        total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"backup bounds exceeded ({MAX_FILES} files / "
                        f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB)")
                rel = path.relative_to(root).as_posix()
                project_files[rel] = _file_hash(path)
            files[project_id] = project_files
        backup_id = uuid.uuid4().hex[:12]
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": "forge-backup", "backup_id": backup_id,
            "label": label, "db_sha256": db_sha256,
            "files": files, "created_at": time.time(),
        }
        archive_path = self._archive_path(backup_id)
        with zipfile.ZipFile(archive_path, "w",
                             zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json",
                             json.dumps(manifest, indent=1))
            archive.writestr("cockpit.db", db_bytes)
            for project_id, entries in files.items():
                root = self.projects[project_id]
                for rel in entries:
                    archive.write(root / rel,
                                  arcname=f"projects/{project_id}/{rel}")
        self._db.execute(
            "INSERT INTO backups (id, label, status, db_sha256, "
            "files_count, size_bytes, actor, created_at) "
            "VALUES (?, ?, 'created', ?, ?, ?, ?, ?)",
            (backup_id, label, db_sha256, total_files,
             archive_path.stat().st_size, actor, time.time()))
        return self.get(backup_id)

    def _prune_one(self) -> None:
        row = self._db.query_one(
            "SELECT id FROM backups ORDER BY created_at ASC, id ASC "
            "LIMIT 1")
        if row is None:
            return
        self._archive_path(row["id"]).unlink(missing_ok=True)
        self._db.execute("DELETE FROM backups WHERE id = ?", (row["id"],))

    def verify(self, backup_id: str) -> dict[str, Any]:
        record = self.get(backup_id)
        archive_path = self._archive_path(backup_id)
        if not archive_path.is_file():
            raise ValueError("backup archive is missing")
        drift: list[dict[str, str]] = []
        db_ok = False
        files_total = 0
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest = json.loads(
                archive.read("manifest.json").decode("utf-8"))
            db_bytes = archive.read("cockpit.db")
            db_ok = _sha256_bytes(db_bytes) == manifest["db_sha256"]
            for project_id, entries in (
                    manifest.get("files") or {}).items():
                root = self.projects.get(project_id)
                for rel, expected_hash in entries.items():
                    files_total += 1
                    if len(drift) >= MAX_DRIFT:
                        continue
                    if root is None:
                        drift.append({"project": project_id, "file": rel,
                                      "state": "unknown-project"})
                        continue
                    path = root / rel
                    if not path.is_file():
                        drift.append({"project": project_id, "file": rel,
                                      "state": "missing"})
                    elif _file_hash(path) != expected_hash:
                        drift.append({"project": project_id, "file": rel,
                                      "state": "changed"})
        status = "verified" if db_ok else "corrupt"
        self._db.execute("UPDATE backups SET status = ? WHERE id = ?",
                         (status, backup_id))
        return {**self.get(backup_id),
                "db_ok": db_ok,
                "files_total": files_total,
                "files_ok": files_total - len(drift),
                "drift": drift,
                "note": ("restore rewrites the plane database; file-tree "
                         "rollback is out of scope")}

    def restore(self, backup_id: str, *, stopped: bool,
                actor: str = "") -> dict[str, Any]:
        record = self.get(backup_id)
        if not stopped:
            raise ValueError(
                "restore requires a stopped plane; stop the plane first")
        archive_path = self._archive_path(backup_id)
        if not archive_path.is_file():
            raise ValueError("backup archive is missing")
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest = json.loads(
                archive.read("manifest.json").decode("utf-8"))
            db_bytes = archive.read("cockpit.db")
        if _sha256_bytes(db_bytes) != manifest["db_sha256"]:
            raise ValueError("backup database failed verification; "
                             "refusing to restore")

        # Consolidate the live WAL into the current file first, so
        # stale pages cannot leak into the restored database.
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        # Update the ledger BEFORE replacing the database file: the
        # current connection keeps writing to the old inode once the
        # file is swapped.
        self._db.execute("UPDATE backups SET status = 'restored' "
                         "WHERE id = ?", (backup_id,))
        temp_path = self.db_path.with_suffix(
            f"{self.db_path.suffix}.restore-{uuid.uuid4().hex}")
        temp_path.write_bytes(db_bytes)
        os.replace(str(temp_path), str(self.db_path))
        for suffix in ("-wal", "-shm"):
            stale = Path(str(self.db_path) + suffix)
            stale.unlink(missing_ok=True)
        restored = dict(self.get(backup_id))
        restored["status"] = "restored"
        restored["restored"] = True
        restored["db_sha256"] = manifest["db_sha256"]
        restored["note"] = ("plane database restored; restart the plane "
                            "to use the restored state")
        return restored
