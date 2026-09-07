"""Exact-file Git staging for autonomous commits (A32.9).

Autonomous commits stage only the explicit, accepted change-set paths — never
``git add .`` / ``git add -A``. Staging rejects Git internals, Forge runtime
state, environment files, credential-like files, and private-key material.
:class:`GitTool.commit_accepted` additionally refuses to commit unless the
acceptance gate succeeded, so a commit is impossible on a rejected candidate.
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any

#: Key material suffixes that must never be staged autonomously.
KEY_FILE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
#: Exact private-key filenames that must never be staged autonomously.
PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ed25519", "id_ecdsa"})

class GitTool:
    def __init__(self, repo: str = ".") -> None:
        self.repo = str(Path(repo).resolve())
    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True, check=False)
    def status(self) -> str: return self.run("status", "--short").stdout.strip()
    def diff(self, staged: bool = False) -> str: return self.run("diff", *( ["--cached"] if staged else [])).stdout
    def changed_files(self) -> list[str]:
        out = self.run("status", "--short", "--untracked-files=all").stdout.splitlines()
        return [line[3:].strip().strip('"') for line in out if len(line) > 3]
    def stage_files(self, files: list[str]) -> None:
        if not files: raise ValueError("Refusing to stage an empty file list")
        root = Path(self.repo)
        safe = []
        for name in sorted(set(files)):
            path = (root / name).resolve()
            try: path.relative_to(root)
            except ValueError: raise ValueError(f"Path outside repository: {name}")
            parts = Path(name).parts
            lower = name.lower()
            if ".git" in parts or name == ".forge" or name.startswith(".forge/"):
                raise ValueError("Git or Forge runtime state cannot be staged")
            if Path(name).name.startswith(".env") or any(token in lower for token in ("credential", "secret", "private_key")):
                raise ValueError("Environment files and credential-like files cannot be staged")
            if Path(name).suffix.lower() in KEY_FILE_SUFFIXES or Path(name).name in PRIVATE_KEY_NAMES:
                raise ValueError("Private key material cannot be staged")
            safe.append(name)
        result = self.run("add", "--", *safe)
        if result.returncode: raise RuntimeError(result.stderr.strip())
    def unstage_files(self, files: list[str]) -> None:
        if files:
            self.run("restore", "--staged", "--", *sorted(set(files)))

    def commit_files(self, files: list[str], message: str) -> subprocess.CompletedProcess[str]:
        self.stage_files(files)
        staged = self.run("diff", "--cached", "--name-only").stdout.splitlines()
        if sorted(staged) != sorted(set(files)):
            self.unstage_files(files)
            raise RuntimeError(f"Staged file set mismatch: expected {sorted(set(files))}, got {sorted(staged)}")
        return self.run("commit", "-m", message)

    def commit_accepted(self, files: list[str], message: str, acceptance: Any) -> subprocess.CompletedProcess[str]:
        """Commit only when the acceptance gate succeeded.

        ``acceptance`` may be an ``AcceptanceDecision``, its ``to_dict()``
        mapping, or a boolean. Anything else that is falsy — or an accepted
        flag that is not true — refuses *before* staging, so a rejected
        candidate can never reach the index.
        """
        if hasattr(acceptance, "accepted"):
            ok = bool(acceptance.accepted)
        elif isinstance(acceptance, dict):
            ok = bool(acceptance.get("accepted", False))
        else:
            ok = bool(acceptance)
        if not ok:
            raise RuntimeError("Refusing to commit: acceptance gate did not succeed")
        return self.commit_files(files, message)
