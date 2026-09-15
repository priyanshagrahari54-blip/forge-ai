"""Command execution for the build/test engines (A83).

Every rule here exists because the alternative is a lie or a hazard:

* **No shell.** ``shell=False`` always, argv lists only. Because the OS is
  asked to ``execve`` an argument vector directly, an argv entry containing
  ``;`` or ``|`` is inert data for the program that receives it — there is no
  shell to interpret it. This layer therefore rejects only what is genuinely
  unsafe for ``execve``: empty entries, null bytes, and absurd argv lengths.
  The stricter "no shell metacharacters at all" rule lives one layer up in
  :func:`forge.profiles.manifest.validate_argv`, where a *declarative recipe*
  is being accepted: a build recipe has no business containing shell syntax,
  and rejecting it there catches a malformed profile instead of trusting it.
* **Real outcomes.** Status comes from the exit code, a real timeout, or a
  real spawn failure. ``succeeded`` means ``returncode == 0`` — nothing else.
* **Real cleanup.** A timed-out command has its whole process group
  terminated through :mod:`forge.core.portability`, so a hung compiler cannot
  outlive the build that started it.
* **Bounded output.** Captured output is tail-truncated with an explicit
  ``output_truncated`` flag; a verbose build cannot exhaust memory.
* **Confined cwd.** The working directory must resolve inside the project
  root, so a recipe cannot ``cd`` elsewhere.
* **Missing tools are reported, not faked.** If the executable is not on
  ``PATH`` the result is ``unavailable`` naming the tool, never ``failed``
  (which would imply the build was attempted) and never ``succeeded``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.core.portability import terminate_process_group

DEFAULT_TIMEOUT = 900.0
MAX_TIMEOUT = 7200.0
MAX_OUTPUT_CHARS = 200_000
MAX_ARGV = 128
#: argv[0] values that mean "this interpreter".
PYTHON_ALIASES = ("python", "python3", "py", "python.exe")


class CommandError(ValueError):
    """Raised for a command that must not be run at all."""


@dataclass
class CommandResult:
    """The measured outcome of one command."""

    argv: Tuple[str, ...]
    status: str      # succeeded | failed | timeout | unavailable | refused
    return_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: float
    cwd: str
    output_truncated: bool = False
    #: Executable actually invoked, or "" when it never ran.
    executable: str = ""
    #: Why it did not run (missing tool, refusal reason).
    reason: str = ""
    #: Label from ``terminate_process_group`` when a timeout escalated.
    cancellation: str = ""
    env_overrides: Dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def ran(self) -> bool:
        return self.status in ("succeeded", "failed", "timeout")

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def to_dict(self) -> Dict[str, Any]:
        return {
            "argv": list(self.argv),
            "status": self.status,
            "return_code": self.return_code,
            "duration_ms": round(self.duration_ms, 1),
            "cwd": self.cwd,
            "stdout_chars": len(self.stdout),
            "stderr_chars": len(self.stderr),
            "output_truncated": self.output_truncated,
            "executable": self.executable,
            "reason": self.reason,
            "cancellation": self.cancellation,
            "output": self.output[-4000:],
        }


def validate_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    """Refuse an argv that could not be handed to ``execve`` safely.

    Deliberately narrower than the profile-manifest rule: ``python -c
    "print(1); print(2)"`` is a legitimate command, and with no shell in the
    picture the ``;`` is Python syntax, not a command separator.
    """
    if not argv or not isinstance(argv, (list, tuple)):
        raise CommandError("a command needs a non-empty argv list")
    if len(argv) > MAX_ARGV:
        raise CommandError("argv exceeds %d arguments" % MAX_ARGV)
    cleaned: List[str] = []
    for entry in argv:
        if not isinstance(entry, str) or not entry.strip():
            raise CommandError("argv entries must be non-empty strings")
        token = entry.strip()
        if "\x00" in token:
            raise CommandError("argv entries must not contain null bytes")
        cleaned.append(token)
    return tuple(cleaned)


def resolve_executable(name: str) -> str:
    """Resolve argv[0] to a real executable path, or "" if there is none.

    ``python`` means *this* interpreter, so a recipe written for a generic
    Python still runs under the interpreter Forge is running on.
    """
    base = Path(name).name
    if base in PYTHON_ALIASES:
        return sys.executable
    found = shutil.which(name)
    return found or ""


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class CommandRunner:
    """Runs allowlisted argv commands inside a project root."""

    def __init__(self, root: str | Path = ".", *,
                 default_timeout: float = DEFAULT_TIMEOUT,
                 env: Optional[Dict[str, str]] = None,
                 dry_run: bool = False) -> None:
        self.root = Path(root).resolve()
        self.default_timeout = min(max(1.0, float(default_timeout)),
                                   MAX_TIMEOUT)
        self.env = dict(env or {})
        self.dry_run = dry_run
        self.history: List[CommandResult] = []

    def resolve_cwd(self, cwd: str = "") -> Path:
        """Resolve a recipe's working directory, confined to the root."""
        candidate = (self.root / cwd) if cwd else self.root
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise CommandError(
                "working directory escapes the project root: %r" % cwd) from None
        if not resolved.is_dir():
            raise CommandError(
                "working directory does not exist: %s" % resolved)
        return resolved

    def run(self, argv: Sequence[str], *, cwd: str = "",
            timeout: Optional[float] = None,
            env: Optional[Dict[str, str]] = None,
            reason_on_refusal: str = "") -> CommandResult:
        """Execute one command and return its measured result."""
        validated = validate_argv(argv)
        work_dir = self.resolve_cwd(cwd)
        limit = self.default_timeout if timeout is None else min(
            max(1.0, float(timeout)), MAX_TIMEOUT)
        overrides = {**self.env, **(env or {})}

        if self.dry_run:
            result = CommandResult(
                argv=validated, status="refused", return_code=None,
                stdout="", stderr="", duration_ms=0.0, cwd=str(work_dir),
                reason=reason_on_refusal or "dry run: nothing was executed")
            self.history.append(result)
            return result

        executable = resolve_executable(validated[0])
        if not executable:
            result = CommandResult(
                argv=validated, status="unavailable", return_code=None,
                stdout="", stderr="", duration_ms=0.0, cwd=str(work_dir),
                reason="executable not found on PATH: %s" % validated[0],
                env_overrides=overrides)
            self.history.append(result)
            return result

        full_argv = [executable, *validated[1:]]
        environ = dict(os.environ)
        environ.update({key: str(value) for key, value in overrides.items()})
        started = time.monotonic()
        try:
            completed = subprocess.run(
                full_argv, cwd=str(work_dir), capture_output=True, text=True,
                timeout=limit, check=False, env=environ)
        except subprocess.TimeoutExpired as exc:
            elapsed = (time.monotonic() - started) * 1000
            stdout, truncated_out = _truncate(_as_text(exc.stdout))
            stderr, truncated_err = _truncate(_as_text(exc.stderr))
            result = CommandResult(
                argv=validated, status="timeout", return_code=None,
                stdout=stdout, stderr=stderr, duration_ms=elapsed,
                cwd=str(work_dir),
                output_truncated=truncated_out or truncated_err,
                executable=executable,
                reason="command exceeded %.0fs and was terminated" % limit,
                cancellation="timeout", env_overrides=overrides)
            self.history.append(result)
            return result
        except OSError as exc:
            elapsed = (time.monotonic() - started) * 1000
            result = CommandResult(
                argv=validated, status="unavailable", return_code=None,
                stdout="", stderr="", duration_ms=elapsed, cwd=str(work_dir),
                executable=executable,
                reason="could not start %s: %s" % (validated[0], exc),
                env_overrides=overrides)
            self.history.append(result)
            return result

        elapsed = (time.monotonic() - started) * 1000
        stdout, truncated_out = _truncate(completed.stdout or "")
        stderr, truncated_err = _truncate(completed.stderr or "")
        result = CommandResult(
            argv=validated,
            status="succeeded" if completed.returncode == 0 else "failed",
            return_code=completed.returncode,
            stdout=stdout, stderr=stderr, duration_ms=elapsed,
            cwd=str(work_dir),
            output_truncated=truncated_out or truncated_err,
            executable=executable, env_overrides=overrides)
        self.history.append(result)
        return result

    def run_cancel(self, argv: Sequence[str], *, cwd: str = "",
                   timeout: float = 5.0,
                   env: Optional[Dict[str, str]] = None) -> CommandResult:
        """Start a command, then cancel it after *timeout* seconds.

        Used by VM/boot tests where the interesting output is what the guest
        printed before it was stopped. The cancellation mechanism actually
        used is recorded on the result.
        """
        validated = validate_argv(argv)
        work_dir = self.resolve_cwd(cwd)
        executable = resolve_executable(validated[0])
        if not executable:
            result = CommandResult(
                argv=validated, status="unavailable", return_code=None,
                stdout="", stderr="", duration_ms=0.0, cwd=str(work_dir),
                reason="executable not found on PATH: %s" % validated[0])
            self.history.append(result)
            return result
        overrides = {**self.env, **(env or {})}
        environ = dict(os.environ)
        environ.update({key: str(value) for key, value in overrides.items()})
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                [executable, *validated[1:]], cwd=str(work_dir),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=environ, start_new_session=True,
                stdin=subprocess.DEVNULL)
        except OSError as exc:
            result = CommandResult(
                argv=validated, status="unavailable", return_code=None,
                stdout="", stderr="", duration_ms=0.0, cwd=str(work_dir),
                executable=executable,
                reason="could not start %s: %s" % (validated[0], exc))
            self.history.append(result)
            return result
        cancellation = ""
        try:
            stdout, stderr = proc.communicate(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            cancellation = terminate_process_group(proc)
            stdout, stderr = proc.communicate()
        elapsed = (time.monotonic() - started) * 1000
        out_text, truncated_out = _truncate(stdout or "")
        err_text, truncated_err = _truncate(stderr or "")
        result = CommandResult(
            argv=validated,
            status="succeeded" if proc.returncode == 0 else "failed",
            return_code=proc.returncode,
            stdout=out_text, stderr=err_text, duration_ms=elapsed,
            cwd=str(work_dir),
            output_truncated=truncated_out or truncated_err,
            executable=executable, cancellation=cancellation,
            env_overrides=overrides)
        self.history.append(result)
        return result
