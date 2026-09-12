"""Rollback for applied self-improvements (A81).

Applying an accepted candidate to the live checkout first snapshots the
exact original bytes of every file it will touch (plus "did not exist"
markers) under ``.forge/self_improvement/snapshots/<candidate>/``. Rollback
restores exactly those files and nothing else — never ``git reset``,
never touching unrelated work. Snapshots are content-addressed so a
tampered snapshot is refused.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from forge.self_improvement.guardrails import Guardrails, GuardrailViolation, normalize_path


class RollbackManager:
    def __init__(self, root: str | Path = ".", *, snapshots: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.snapshots = snapshots or (self.root / ".forge" / "self_improvement" / "snapshots")

    def _dir(self, candidate_id: str) -> Path:
        safe = "".join(ch for ch in candidate_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("invalid candidate id")
        return self.snapshots / safe

    # -- apply with snapshot ----------------------------------------------------

    def apply(self, candidate_id: str, changes: dict[str, str], *,
              guardrails: Guardrails | None = None) -> dict[str, Any]:
        """Snapshot originals, then write ``changes`` to the live root."""
        rails = guardrails or Guardrails()
        originals: dict[str, str | None] = {}
        for raw in changes:
            path = normalize_path(raw)
            target = self.root / path
            if target.is_file():
                try:
                    originals[path] = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    originals[path] = None
            else:
                originals[path] = None
        violations = rails.check_changes({normalize_path(k): v for k, v in changes.items()}, originals)
        if violations:
            raise GuardrailViolation(violations)
        directory = self._dir(candidate_id)
        if directory.exists():
            raise FileExistsError(f"snapshot for {candidate_id} already exists")
        directory.mkdir(parents=True)
        manifest: dict[str, Any] = {"candidate_id": candidate_id, "created_at": time.time(),
                                    "files": {}}
        for path, original in originals.items():
            entry: dict[str, Any] = {"existed": original is not None}
            if original is not None:
                blob = original.encode("utf-8")
                digest = hashlib.sha256(blob).hexdigest()
                (directory / (digest + ".blob")).write_bytes(blob)
                entry["sha256"] = digest
                entry["size"] = len(blob)
            manifest["files"][path] = entry
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        written: list[str] = []
        for raw, content in changes.items():
            path = normalize_path(raw)
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(path)
        return {"candidate_id": candidate_id, "files": sorted(written),
                "snapshot": str(directory)}

    # -- rollback ---------------------------------------------------------------

    def can_rollback(self, candidate_id: str) -> bool:
        return (self._dir(candidate_id) / "manifest.json").is_file()

    def rollback(self, candidate_id: str) -> dict[str, Any]:
        directory = self._dir(candidate_id)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"no snapshot for {candidate_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored: list[str] = []
        removed: list[str] = []
        for path, entry in manifest.get("files", {}).items():
            target = self.root / normalize_path(path)
            if entry.get("existed"):
                blob_path = directory / (entry["sha256"] + ".blob")
                blob = blob_path.read_bytes()
                if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
                    raise ValueError(f"snapshot blob for {path} is corrupt; refusing rollback")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
                restored.append(path)
            elif target.exists():
                target.unlink()
                removed.append(path)
                parent = target.parent
                while parent != self.root and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        (directory / "rolled_back.json").write_text(
            json.dumps({"at": time.time(), "restored": restored, "removed": removed}),
            encoding="utf-8")
        return {"candidate_id": candidate_id, "restored": sorted(restored),
                "removed": sorted(removed)}

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.snapshots.is_dir():
            return rows
        for directory in sorted(self.snapshots.iterdir()):
            manifest = directory / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except ValueError:
                continue
            rows.append({"candidate_id": data.get("candidate_id", directory.name),
                         "created_at": data.get("created_at"),
                         "files": sorted(data.get("files", {})),
                         "rolled_back": (directory / "rolled_back.json").is_file()})
        return rows

    def discard(self, candidate_id: str) -> None:
        shutil.rmtree(self._dir(candidate_id), ignore_errors=True)
