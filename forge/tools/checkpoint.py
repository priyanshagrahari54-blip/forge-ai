"""File-level checkpoints that preserve the exact pre-change worktree (A32.8).

Before a change set applies, the checkpoint captures the exact original bytes
of affected files, records hashes, sizes, and permission bits, and notes
which declared paths did not exist — enough metadata to restore the candidate
set exactly. Rollback restores *only* files belonging to the candidate change
set: it never runs ``git reset --hard``, never deletes unrelated user files,
and never discards unrelated modifications.
"""
from __future__ import annotations
import hashlib, json, shutil, stat, tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from forge.tools.git import GitTool
from forge.security.verification import is_excluded

@dataclass
class Checkpoint:
    id: str
    root: Path
    snapshot: Path
    files: dict[str, str | None]
    #: Per-path restore metadata: ``existed`` plus, for captured files,
    #: ``sha256``/``size``/``mode`` evidence.
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)

class CheckpointManager:
    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()
        self.git = GitTool(str(self.root))
    def create(self, label: str = "change", declared: list[str] | None = None) -> Checkpoint:
        snapshot = Path(tempfile.mkdtemp(prefix="forge-checkpoint-"))
        files: dict[str, str | None] = {}
        meta: dict[str, dict[str, Any]] = {}
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            relative = p.relative_to(self.root)
            # Skip runtime state, virtualenvs, and caches so checkpoints only
            # hold application content (also keeps them fast and bounded).
            if is_excluded(relative.parts):
                continue
            rel = relative.as_posix()
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            files[rel] = digest
            info = p.stat()
            meta[rel] = {"existed": True, "sha256": digest, "size": len(content),
                         "mode": stat.S_IMODE(info.st_mode)}
            target = snapshot / rel; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, target)
        # Declared-but-missing paths are recorded explicitly so rollback knows
        # they must be deleted rather than restored.
        for name in declared or []:
            if name not in files and name not in meta:
                meta[name] = {"existed": False}
        ident = hashlib.sha256((label + json.dumps(files, sort_keys=True)).encode()).hexdigest()[:16]
        return Checkpoint(ident, self.root, snapshot, files, meta)
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
