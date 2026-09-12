"""Portable process signalling (POSIX and Windows, 32-bit included).

Windows does not define ``signal.SIGKILL`` and has no ``os.killpg``, so the
obvious POSIX spellings raise ``AttributeError`` there — and ``AttributeError``
is not caught by the ``except OSError`` handlers that usually surround this
code, so it escapes as a crash instead of a structured failure.

This module is the single place in Forge that names a platform-specific signal
member. Everything else must go through :func:`stop_signal` or
:func:`terminate_process_group`, which are safe on every platform Forge runs on
(including the Python 3.8 / Windows 7 32-bit desktop target).

Windows semantics, stated honestly rather than papered over: ``os.kill`` treats
every signal other than ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT`` as an
unconditional ``TerminateProcess``. There is therefore no graceful terminate
via ``os.kill`` on Windows — a forced stop is the only real option, and callers
report exactly that instead of claiming a POSIX signal was delivered.
"""
from __future__ import annotations

import os
import signal
import subprocess

__all__ = ["MINIMUM_PYTHON", "MINIMUM_PYTHON_STRING", "HAS_SIGKILL",
           "HAS_KILLPG", "stop_signal", "terminate_process_group"]

#: The interpreter floor Forge actually supports, mirroring
#: ``requires-python`` in ``pyproject.toml``. This is the single source of
#: truth for user-facing "your Python is too old" messages; the companion
#: test in ``tests/test_python38_compat.py`` asserts it matches the
#: packaging metadata, so it cannot silently drift.
MINIMUM_PYTHON = (3, 8)

#: The same floor rendered for humans, e.g. ``"3.8"``.
MINIMUM_PYTHON_STRING = ".".join(str(p) for p in MINIMUM_PYTHON)

#: ``False`` on Windows, where the member does not exist at all.
HAS_SIGKILL = hasattr(signal, "SIGKILL")

#: ``False`` on Windows, which has no process groups to signal.
HAS_KILLPG = hasattr(os, "killpg")


def stop_signal(op: str) -> tuple[int, str]:
    """Map a process operation to a signal this platform actually defines.

    ``op`` is ``"terminate"`` or ``"kill"``. Returns ``(signum, label)`` where
    ``label`` is what the caller should report back. On POSIX ``kill``
    escalates to ``SIGKILL``; on Windows both operations map to ``SIGTERM``,
    which ``os.kill`` delivers as ``TerminateProcess``. The label never claims
    a signal the platform does not have.
    """
    if op != "terminate" and HAS_SIGKILL:
        return signal.SIGKILL, "SIGKILL"
    return signal.SIGTERM, "SIGTERM"


def _own_process_group(proc: subprocess.Popen) -> int:
    """Return the child's pgid, but only when the child leads that group.

    A child spawned with ``start_new_session=True`` is its own group leader,
    so ``pgid == pid`` and signalling the group is both what the caller wants
    and safe. Any other child shares its parent's group, and signalling that
    would kill the caller too — so in that case no group is reported and the
    caller falls back to signalling the process alone.
    """
    if not HAS_KILLPG:
        return 0
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return 0  # already gone, or the platform refused the lookup
    return pgid if pgid == proc.pid else 0


def terminate_process_group(proc: subprocess.Popen,
                            grace: float = 2.0) -> str:
    """Cancel ``proc`` and, where the OS allows it, its whole process group.

    POSIX, child is a group leader: ``SIGTERM`` the group, wait ``grace``
    seconds, escalate to ``SIGKILL``. POSIX child that shares our group,
    and Windows (no groups, no ``SIGKILL``): ``terminate()`` then
    ``kill()`` on the process itself.

    Returns a short label naming the mechanism actually used, so a caller can
    tell an escalation from a fallback without inspecting the platform. Never
    raises for a missing signal member, a missing process group, or an
    already-dead process.
    """
    pgid = _own_process_group(proc)
    if pgid and HAS_SIGKILL:
        attempted = "none"
        for signum, label in ((signal.SIGTERM, "SIGTERM"),
                              (signal.SIGKILL, "SIGKILL")):
            attempted = label
            try:
                os.killpg(pgid, signum)
                proc.wait(timeout=grace)
                return label
            except (OSError, subprocess.TimeoutExpired):
                continue
        return attempted

    for action in (proc.terminate, proc.kill):
        try:
            action()
            proc.wait(timeout=grace)
            return action.__name__
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "kill"
