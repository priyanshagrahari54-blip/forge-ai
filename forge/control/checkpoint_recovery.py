"""Safe reconstruction of durable checkpoint objects after restart."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from forge.security.verification import is_excluded
from forge.tools.checkpoint import Checkpoint


def restore_available_checkpoints(plane: Any) -> dict[str, list[str]]:
    """Rebuild available checkpoint objects from durable metadata/snapshots.

    Only snapshots stored under each project's own ``.forge/checkpoints``
    directory are accepted. Missing or misplaced snapshots are marked
    unavailable rather than loading an arbitrary filesystem path.
    """
    restored: list[str] = []
    unavailable: list[str] = []
    rows = plane._db.query(
        "SELECT * FROM checkpoints WHERE status = 'available' "
        "ORDER BY created_at DESC"
    )
    for row in rows:
        try:
            project = plane.get_project(row["project_id"])
            root = Path(project.root).resolve()
            allowed_root = (root / ".forge" / "checkpoints").resolve()
            snapshot = Path(row["snapshot_path"]).resolve()
            try:
                snapshot.relative_to(allowed_root)
            except ValueError:
                raise ValueError("checkpoint snapshot outside allowed root")
            if not snapshot.is_dir():
                raise ValueError("checkpoint snapshot is missing")
            files: dict[str, str] = {}
            for path in snapshot.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(snapshot)
                if is_excluded(relative.parts):
                    continue
                data = path.read_bytes()
                files[relative.as_posix()] = hashlib.sha256(data).hexdigest()
            checkpoint = Checkpoint(
                id=str(row["id"]),
                root=root,
                snapshot=snapshot,
                files=files,
            )
            with plane._checkpoints_lock:
                plane._checkpoints[checkpoint.id] = checkpoint
            restored.append(checkpoint.id)
        except Exception:
            unavailable.append(str(row["id"]))
            plane._db.execute(
                "UPDATE checkpoints SET status = 'unavailable' WHERE id = ?",
                (row["id"],))
    return {"restored": restored, "unavailable": unavailable}
