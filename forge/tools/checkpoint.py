from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from forge.tools.git import GitTool


@dataclass
class Snapshot:
    id: str
    description: str
    files: dict[str, str | None]  # relative path -> content (None if file didn't exist)


class CheckpointManager:
    """Manages file checkpoints and automatic rollbacks."""

    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).resolve()
        self.git = GitTool(str(self.root))
        self.snapshots: dict[str, Snapshot] = {}

    def create_checkpoint(
        self,
        target_files: tuple[str, ...] | list[str] = (),
        description: str = "",
    ) -> str:
        checkpoint_id = f"chk-{uuid.uuid4().hex[:8]}"

        files_snapshot: dict[str, str | None] = {}
        if target_files:
            for rel_path in target_files:
                target = (self.root / rel_path).resolve()
                if target.exists() and target.is_file():
                    files_snapshot[rel_path] = target.read_text(encoding="utf-8")
                else:
                    files_snapshot[rel_path] = None
        else:
            for path in self.root.rglob("*"):
                if path.is_file() and not any(
                    part.startswith(".") for part in path.parts if part != "."
                ):
                    try:
                        rel = str(path.relative_to(self.root))
                        files_snapshot[rel] = path.read_text(encoding="utf-8")
                    except Exception:
                        pass

        self.snapshots[checkpoint_id] = Snapshot(
            id=checkpoint_id,
            description=description,
            files=files_snapshot,
        )
        return checkpoint_id

    def rollback(self, checkpoint_id: str) -> None:
        snapshot = self.snapshots.get(checkpoint_id)
        if snapshot is None:
            self.git.run("checkout", "--", ".")
            self.git.run("clean", "-fd")
            return

        for rel_path, content in snapshot.files.items():
            target = (self.root / rel_path).resolve()
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

    def commit_checkpoint(self, checkpoint_id: str, message: str = "") -> None:
        if (self.root / ".git").exists():
            self.git.add(".")
            msg = message or f"Checkpoint commit {checkpoint_id}"
            self.git.commit(msg)
