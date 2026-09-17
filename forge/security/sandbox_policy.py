"""Declarative execution sandbox policy for Forge jobs.

This module does not pretend to provide OS-level isolation by itself. It
produces a bounded policy that an execution backend must enforce. Backends
that cannot enforce a requested control must fail closed instead of silently
downgrading the policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


@dataclass(frozen=True)
class SandboxPolicy:
    """Limits and permissions required for one autonomous execution."""

    root: Path
    timeout_seconds: float = 300.0
    max_output_bytes: int = 1_000_000
    max_file_bytes: int = 50_000_000
    max_processes: int = 32
    network: str = "deny"
    writable_paths: Tuple[str, ...] = ()
    allow_subprocess: bool = True
    allow_secrets: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_bytes <= 0 or self.max_file_bytes <= 0:
            raise ValueError("output and file limits must be positive")
        if self.max_processes < 1:
            raise ValueError("max_processes must be positive")
        if self.network not in ("deny", "allowlist"):
            raise ValueError("network must be deny or allowlist")
        root = self.root.resolve()
        for raw in self.writable_paths:
            path = (root / raw).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("writable path escapes sandbox root") from exc

    def allows_path(self, path: str | Path, *, write: bool = False) -> bool:
        """Check whether a path is inside the sandbox and writable if needed."""
        root = self.root.resolve()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return False
        if not write:
            return True
        if not self.writable_paths:
            return False
        return any(
            relative == Path(item) or Path(item) in relative.parents
            for item in self.writable_paths
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "root": str(self.root.resolve()),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_processes": self.max_processes,
            "network": self.network,
            "writable_paths": list(self.writable_paths),
            "allow_subprocess": self.allow_subprocess,
            "allow_secrets": self.allow_secrets,
            "isolation_note": "Declarative policy; execution backend must enforce it.",
        }
