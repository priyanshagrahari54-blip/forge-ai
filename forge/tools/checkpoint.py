"""File-level checkpoints that preserve the exact pre-change worktree."""
from __future__ import annotations
import hashlib, json, shutil, tempfile
from pathlib import Path
from dataclasses import dataclass
from forge.tools.git import GitTool
from forge.security.verification import is_excluded

@dataclass
class Checkpoint:
    id: str
    root: Path
    snapshot: Path
    files: dict[str, str | None]

class CheckpointManager:
    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()
        self.git = GitTool(str(self.root))
    def create(self, label: str = "change") -> Checkpoint:
        snapshot = Path(tempfile.mkdtemp(prefix="forge-checkpoint-"))
        files: dict[str, str | None] = {}
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            relative = p.relative_to(self.root)
            # Skip runtime state, virtualenvs, and caches so checkpoints only
            # hold application content (also keeps them fast and bounded).
            if is_excluded(relative.parts):
                continue
            rel = relative.as_posix()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            files[rel] = digest
            target = snapshot / rel; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, target)
        ident = hashlib.sha256((label + json.dumps(files, sort_keys=True)).encode()).hexdigest()[:16]
        return Checkpoint(ident, self.root, snapshot, files)
    def rollback(self, checkpoint: Checkpoint, changed_files: list[str] | None = None) -> None:
        current = {p.relative_to(self.root).as_posix(): p for p in self.root.rglob("*") if p.is_file() and not is_excluded(p.relative_to(self.root).parts)}
        # Restore only declared candidate paths. A caller that does not know its
        # paths can still restore modified pre-existing files, but we never
        # delete an unknown untracked file belonging to a user.
        allowed = set(changed_files or [])
        for rel, source in ((r, checkpoint.snapshot / r) for r in checkpoint.files):
            if changed_files is not None and rel not in allowed: continue
            target = self.root / rel
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == checkpoint.files[rel]: continue
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
        for rel, target in current.items():
            if rel not in checkpoint.files and rel in allowed:
                target.unlink()
                parent = target.parent
                while parent != self.root and not any(parent.iterdir()): parent.rmdir(); parent = parent.parent
        self.cleanup(checkpoint)
    def cleanup(self, checkpoint: Checkpoint) -> None:
        shutil.rmtree(checkpoint.snapshot, ignore_errors=True)
