"""Bounded LOCAL execution for the lightweight client (A81, req 3 & 10).

The G560 executes **only** these fixed, read-only operations — there is
no endpoint, no plugin hook, and no way to run anything else:

- ``repo_summary``  — bounded file walk (counts by extension, total
  bytes, Python file count);
- ``git_status``    — ``git status --porcelain`` (argv list, no shell,
  bounded output, explicit timeout);
- ``todo_scan``     — count of ``TODO``/``FIXME`` markers (bounded).

Every operation:

- is a *local* read on a directory configured on this machine;
- walks at most ``policy.max_files_walked`` files and returns bounded
  output;
- never spawns a shell, never runs model code, never writes.

Anything else raises :class:`~forge.link.errors.LocalExecutionRefused`
(fail closed). This is what keeps the laptop lightweight while the
server does the heavy engineering.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from forge.link.errors import LocalExecutionRefused

#: The complete LOCAL operation vocabulary. Closed set on purpose.
OPERATIONS = ("repo_summary", "git_status", "todo_scan")

#: Bound the subprocess runtimes (seconds) and output sizes (bytes).
GIT_TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_BYTES = 64 * 1024
_DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".forge", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
})


def _iter_files(root: Path, skip: frozenset, limit: int):
    """Lazily yield up to ``limit`` file paths under ``root``.

    Bounded walk: never materializes the tree (a huge repo costs one
    ``limit``-sized list, not the whole hierarchy), skips junk/venv
    directories, tolerates unreadable dirs, and never follows symlink
    loops (it does not descend into symlinked directories).
    """
    stack = [root]
    yielded = 0
    while stack and yielded < limit:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current),
                             key=lambda entry: entry.name)
        except OSError:
            continue  # unreadable directory: skip, keep walking
        directories = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue  # never follow symlinks: no loops, no escapes
                if entry.is_dir():
                    if entry.name not in skip:
                        directories.append(Path(entry.path))
                    continue
                if entry.is_file():
                    yield Path(entry.path)
                    yielded += 1
                    if yielded >= limit:
                        return
            except OSError:
                continue
        stack.extend(reversed(directories))  # depth-first, name order


class LocalExecutor:
    """Executes the fixed light operations on a local project root."""

    def __init__(self, policy, *, skip_dirs: Optional[frozenset] = None,
                 meminfo_path: str = "/proc/meminfo") -> None:
        self.policy = policy
        self.skip_dirs = skip_dirs or _DEFAULT_SKIP_DIRS
        self._meminfo_path = meminfo_path

    def supported(self, operation: str) -> bool:
        return operation in OPERATIONS

    def run(self, operation: str, root: str) -> dict[str, Any]:
        """Run one fixed operation. Everything else is refused."""
        if operation not in OPERATIONS:
            raise LocalExecutionRefused(
                f"unknown local operation {operation!r}; allowed: "
                f"{', '.join(OPERATIONS)}")
        directory = Path(root).expanduser()
        if not directory.is_dir():
            raise LocalExecutionRefused(
                f"project root is not a directory: {root}")
        if operation == "repo_summary":
            return self._repo_summary(directory)
        if operation == "git_status":
            return self._git_status(directory)
        return self._todo_scan(directory)

    # -- repo_summary ---------------------------------------------------------

    def _repo_summary(self, directory: Path) -> dict[str, Any]:
        max_files = max(1, self.policy.max_files_walked)
        counts: dict[str, int] = {}
        total_bytes = 0
        python_files = 0
        walked = 0
        truncated = False
        for path in _iter_files(directory, self.skip_dirs, max_files + 1):
            if walked >= max_files:
                truncated = True
                break
            walked += 1
            suffix = path.suffix.lower() or "(none)"
            counts[suffix] = counts.get(suffix, 0) + 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
            if suffix == ".py":
                python_files += 1
        top = sorted(counts.items(), key=lambda item: (-item[1],
                                                       item[0]))[:12]
        return {
            "operation": "repo_summary",
            "root": str(directory),
            "files_seen": walked,
            "truncated": truncated,
            "total_bytes": total_bytes,
            "python_files": python_files,
            "top_extensions": [{"ext": ext, "count": count}
                               for ext, count in top],
        }

    # -- git_status -------------------------------------------------------------

    def _git_status(self, directory: Path) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(directory),
                capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
                shell=False)
        except FileNotFoundError:
            return {"operation": "git_status", "available": False,
                    "reason": "git not installed"}
        except subprocess.TimeoutExpired:
            raise LocalExecutionRefused("git status timed out") from None
        except OSError as exc:
            raise LocalExecutionRefused(f"git status failed: {exc}") from None
        raw = completed.stdout[:MAX_OUTPUT_BYTES].decode("utf-8",
                                                         errors="replace")
        lines = [line for line in raw.splitlines() if line.strip()]
        return {
            "operation": "git_status",
            "available": completed.returncode == 0,
            "exit_code": completed.returncode,
            "entries": lines[:200],
            "dirty_lines": len(lines),
            "truncated": len(raw) >= MAX_OUTPUT_BYTES,
        }

    # -- todo_scan ---------------------------------------------------------------

    def _todo_scan(self, directory: Path) -> dict[str, Any]:
        max_files = max(1, self.policy.max_files_walked)
        todo = fixme = 0
        walked = 0
        truncated = False
        for path in _iter_files(directory, self.skip_dirs, max_files + 1):
            if path.suffix.lower() != ".py":
                continue
            if walked >= max_files:
                truncated = True
                break
            walked += 1
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue  # skip absurd files, keep scanning
                text = path.read_text(encoding="utf-8",
                                      errors="replace")[:512 * 1024]
            except OSError:
                continue
            todo += text.count("TODO")
            fixme += text.count("FIXME")
        return {"operation": "todo_scan", "files_scanned": walked,
                "todo": todo, "fixme": fixme, "truncated": truncated}
