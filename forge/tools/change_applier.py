"""Controlled code-change application layer (A32.4).

The single place where model-produced changes become repository writes. It
validates every path and payload, enforces permissions through the
``ToolRuntime``, records every changed path, and (when a ``CheckpointManager``
is provided) checkpoints before the first modification so a change set can be
rolled back exactly.

Model output never bypasses this layer: the coder and debugger write through
it, and the supervisor rolls back through the same recorded path set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

from forge.runtime.runtime import ToolRuntime

#: Hard bound on a single generated file, so a runaway model cannot produce an
#: unbounded write.
MAX_FILE_BYTES = 2 * 1024 * 1024

_SECRET_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

_CREDENTIAL_FILENAME_TOKENS = ("credential", "secret", "private_key", "token")


@dataclass(frozen=True)
class CodeChange:
    """One validated, model-proposed repository change."""

    path: str
    content: str
    action: str = "modify"  # create | modify


@dataclass
class ApplyResult:
    """Outcome of applying a change set through the controlled layer."""

    changed_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checkpoint_id: str | None = None
    checkpoint: Any = None  # internal: the live Checkpoint for rollback

    @property
    def success(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": list(self.changed_paths),
            "errors": list(self.errors),
            "checkpoint_id": self.checkpoint_id,
        }


class ChangeApplier:
    """Validate, checkpoint, and apply model changes through the runtime."""

    def __init__(self, runtime: ToolRuntime, checkpoint_manager=None) -> None:
        self.runtime = runtime
        self.checkpoint_manager = checkpoint_manager

    # -- validation -------------------------------------------------------

    def validate(self, change: CodeChange) -> None:
        """Raise ``ValueError`` for any unsafe or malformed change.

        Independent of the coder's own structural validation so the write layer
        is safe even if a future caller forgets an upstream check.
        """
        self._validate_path(change.path)
        if change.action not in ("create", "modify"):
            raise ValueError(f"Unsupported change action for {change.path!r}: {change.action!r}")
        self._validate_content(change.path, change.content)

    @staticmethod
    def _validate_path(path: str) -> None:
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", path):
            raise ValueError(f"Change path must be relative: {path!r}")
        if ".." in candidate.parts or ".git" in candidate.parts or ".forge" in candidate.parts:
            raise ValueError(f"Change path is outside the permitted source tree: {path!r}")
        if "\\" in path:
            raise ValueError(f"Change path must use repository-relative POSIX separators: {path!r}")

    @classmethod
    def _validate_content(cls, path: str, content: str) -> None:
        name = PurePosixPath(path).name.lower()
        if name == ".env" or name.endswith(".env"):
            raise ValueError(f"Environment files cannot be written autonomously: {path!r}")
        if any(token in name for token in _CREDENTIAL_FILENAME_TOKENS):
            raise ValueError(f"Credential-like files cannot be written autonomously: {path!r}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f"Change exceeds {MAX_FILE_BYTES} bytes: {path}")
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"Change appears to contain a secret: {path}")
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                raise ValueError(f"Change produced invalid Python for {path}: {exc}") from exc

    # -- application ------------------------------------------------------

    def apply(self, changes: Iterable[CodeChange | dict[str, str]], approved: bool,
              label: str = "change") -> ApplyResult:
        """Checkpoint, validate, and apply a change set.

        A checkpoint is created before the first write when a
        ``CheckpointManager`` is configured, so the pre-change state can be
        restored exactly. Paths are recorded in application order; unrelated
        files are never touched.
        """
        normalized = [
            change if isinstance(change, CodeChange)
            else CodeChange(path=change["path"], content=change["content"], action=change.get("action", "modify"))
            for change in changes
        ]
        result = ApplyResult()
        if self.checkpoint_manager is not None:
            checkpoint = self.checkpoint_manager.create(label)
            result.checkpoint_id = checkpoint.id
            result.checkpoint = checkpoint

        for change in normalized:
            try:
                self.validate(change)
            except ValueError as exc:
                result.errors.append(str(exc))
                continue
            write = self.runtime.execute(
                "write_file", approved=approved, path=change.path, content=change.content
            )
            if not write.success:
                result.errors.append(f"Failed to write {change.path}: {write.error}")
                continue
            if change.path not in result.changed_paths:
                result.changed_paths.append(change.path)

        if result.checkpoint is not None and not result.success:
            # Restore exactly the files touched so a failed apply never leaves
            # a half-applied change set behind.
            self.checkpoint_manager.rollback(result.checkpoint, result.changed_paths)
            self.checkpoint_manager.cleanup(result.checkpoint)
            result.checkpoint_id = None
            result.checkpoint = None
        return result

    def rollback(self, result: ApplyResult) -> None:
        """Restore the checkpoint captured for a successful ``ApplyResult``."""
        if self.checkpoint_manager is None or result.checkpoint is None:
            return
        self.checkpoint_manager.rollback(result.checkpoint, result.changed_paths)
        self.checkpoint_manager.cleanup(result.checkpoint)
        result.checkpoint = None
        result.checkpoint_id = None
        result.changed_paths = []
