from __future__ import annotations

import subprocess
from pathlib import Path


class GitTool:
    def __init__(self, repo: str = ".") -> None:
        self.repo = str(Path(repo).resolve())

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def init(self) -> str:
        res = self.run("init")
        return res.stdout.strip()

    def status(self) -> str:
        result = self.run("status", "--short")
        return result.stdout.strip()

    def diff(self) -> str:
        result = self.run("diff")
        return result.stdout.strip()

    def add(self, *paths: str) -> str:
        if not paths:
            paths = (".",)
        result = self.run("add", *paths)
        return result.stdout.strip()

    def commit(self, message: str) -> str:
        result = self.run("commit", "-m", message)
        return result.stdout.strip()

    def checkout(self, *args: str) -> str:
        result = self.run("checkout", *args)
        return result.stdout.strip()

    def stash(self, *args: str) -> str:
        result = self.run("stash", *args)
        return result.stdout.strip()
