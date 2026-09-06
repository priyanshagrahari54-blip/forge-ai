import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Set

from forge.tools.git import GitTool


@dataclass
class CheckpointSnapshot:
    checkpoint_id: str
    head_commit: str
    initial_untracked_files: Set[str] = field(default_factory=set)
    initial_tracked_modified: Set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)


class CheckpointManager:
    """Manages exact state capturing and target-based explicit rollback."""

    def __init__(self, root: str | Path = ".", git_tool: GitTool | None = None) -> None:
        self.root = Path(root).resolve()
        self.git_tool = git_tool or GitTool(repo=str(self.root))

    def create_checkpoint(
        self, checkpoint_id: str, candidate_metadata: dict[str, Any] | None = None
    ) -> CheckpointSnapshot:
        head_commit = self._get_head_commit()
        untracked = set(self._get_untracked_files())
        modified = set(self._get_modified_tracked_files())

        return CheckpointSnapshot(
            checkpoint_id=checkpoint_id,
            head_commit=head_commit,
            initial_untracked_files=untracked,
            initial_tracked_modified=modified,
            metadata=candidate_metadata or {},
        )

    def rollback(self, snapshot: CheckpointSnapshot) -> None:
        """Restores repository state explicitly to snapshot state."""
        # 1. Reset tracked files to snapshot.head_commit
        if snapshot.head_commit:
            self.git_tool.run("reset", "--hard", snapshot.head_commit)

        # 2. Remove any newly created untracked files/directories
        current_untracked = set(self._get_untracked_files())
        newly_created = current_untracked - snapshot.initial_untracked_files

        for rel_path in newly_created:
            if rel_path.startswith(".forge/self/history") or rel_path.startswith(
                ".forge/memory"
            ):
                continue

            target_path = self.root / rel_path
            if target_path.is_file() or target_path.is_symlink():
                try:
                    target_path.unlink()
                except Exception:
                    pass
            elif target_path.is_dir():
                try:
                    shutil.rmtree(target_path)
                except Exception:
                    pass

        # 3. Clean untracked changes excluding .forge
        self.git_tool.run("clean", "-fd", "-e", ".forge")

    def _get_head_commit(self) -> str:
        proc = self.git_tool.run("rev-parse", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _get_untracked_files(self) -> List[str]:
        proc = self.git_tool.run("ls-files", "--others", "--exclude-standard")
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _get_modified_tracked_files(self) -> List[str]:
        proc = self.git_tool.run("diff", "--name-only")
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
