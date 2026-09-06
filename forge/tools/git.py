from __future__ import annotations
import subprocess
from pathlib import Path

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
