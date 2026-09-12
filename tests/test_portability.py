"""Process-signalling portability (POSIX + Windows) and a static guard.

Two kinds of coverage:

1. **Behavioural.** ``stop_signal`` / ``terminate_process_group`` are exercised
   on this machine and then re-exercised with the platform capability flags
   forced off, which is exactly the state a Windows host reports. The Windows
   branch is therefore really executed, not merely reasoned about — it is the
   branch that used to raise ``AttributeError: module 'signal' has no
   attribute 'SIGKILL'``.

2. **Static.** No module outside ``forge.core.portability`` may name a signal
   or ``os`` member that does not exist on Windows. Those raise
   ``AttributeError``, which the ``except OSError`` handlers around this kind of
   code do not catch, so they surface as crashes rather than structured
   failures. This runs on any platform, so CI catches a regression on Linux.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge.core import portability
from forge.core.portability import stop_signal, terminate_process_group

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "forge" / "core" / "portability.py"

#: Signal members that do not exist on Windows. SIGTERM/SIGINT/SIGBREAK/
#: SIGABRT/SIGFPE/SIGILL/SIGSEGV do, so they are not listed.
WINDOWS_MISSING_SIGNALS = {
    "SIGALRM", "SIGCHLD", "SIGCONT", "SIGHUP", "SIGIO", "SIGKILL", "SIGPIPE",
    "SIGPROF", "SIGPWR", "SIGQUIT", "SIGSTOP", "SIGSYS", "SIGTRAP", "SIGTSTP",
    "SIGTTIN", "SIGTTOU", "SIGURG", "SIGUSR1", "SIGUSR2", "SIGVTALRM",
    "SIGWINCH", "SIGXCPU", "SIGXFSZ",
}

#: ``os`` members that do not exist on Windows.
WINDOWS_MISSING_OS = {
    "chown", "confstr", "ctermid", "fchown", "fork", "forkpty", "getegid",
    "geteuid", "getgid", "getloadavg", "getpgid", "getpriority", "getsid",
    "getuid", "killpg", "lchown", "major", "makedev", "minor", "mkfifo",
    "mknod", "nice", "posix_openpt", "sched_get_priority_max",
    "sched_get_priority_min", "sched_getaffinity", "sched_getparam",
    "sched_getscheduler", "sched_setaffinity", "sched_setparam",
    "sched_setscheduler", "sched_yield", "setegid", "seteuid", "setgid",
    "setgroups", "setpgid", "setpgrp", "setpriority", "setregid", "setreuid",
    "setsid", "setuid", "sysconf", "ttyname", "uname", "wait3", "wait4",
    "WCOREDUMP", "WEXITSTATUS", "WIFCONTINUED", "WIFEXITED", "WIFSIGNALED",
    "WIFSTOPPED", "WSTOPSIG", "WTERMSIG",
}

WINDOWS_MISSING = (("signal", WINDOWS_MISSING_SIGNALS),
                   ("os", WINDOWS_MISSING_OS))


# ---------------------------------------------------------------------------
# stop_signal
# ---------------------------------------------------------------------------

def test_stop_signal_escalates_to_sigkill_on_posix(monkeypatch):
    """On a platform that has SIGKILL, 'kill' must use it."""
    monkeypatch.setattr(portability, "HAS_SIGKILL", True)
    monkeypatch.setattr(portability.signal, "SIGKILL", 9, raising=False)
    signum, label = stop_signal("kill")
    assert (signum, label) == (9, "SIGKILL")
    assert stop_signal("terminate") == (portability.signal.SIGTERM, "SIGTERM")


def test_stop_signal_never_names_a_missing_member(monkeypatch):
    """Windows has no SIGKILL; 'kill' must degrade, not raise.

    This is the regression test for the AttributeError that
    ``LocalDesktopProvider.process`` used to raise on Windows.
    """
    monkeypatch.setattr(portability, "HAS_SIGKILL", False)
    signum, label = stop_signal("kill")
    assert label == "SIGTERM"
    assert signum == portability.signal.SIGTERM
    assert stop_signal("terminate")[1] == "SIGTERM"


@pytest.mark.parametrize("op", ["terminate", "kill", "poll", "anything"])
def test_stop_signal_always_returns_a_real_signal_number(monkeypatch, op):
    monkeypatch.setattr(portability, "HAS_SIGKILL", False)
    signum, label = stop_signal(op)
    assert isinstance(signum, int)
    assert isinstance(label, str) and label


# ---------------------------------------------------------------------------
# terminate_process_group
# ---------------------------------------------------------------------------

def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])


def test_group_leader_is_cancelled_via_the_process_group():
    """A ``start_new_session`` child owns its group, so the group is killed.

    This is how ``forge.compute.remote`` spawns its ssh client, and it is the
    path the existing SSH-timeout regression test depends on.
    """
    if not portability.HAS_KILLPG:
        pytest.skip("platform has no process groups")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True)
    try:
        label = terminate_process_group(proc, grace=5.0)
        assert proc.poll() is not None, "group leader survived cancellation"
        assert label in ("SIGTERM", "SIGKILL"), label
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_shared_group_child_is_signalled_alone_never_by_group():
    """A child in *our* group must not be cancelled via killpg.

    Signalling that group would terminate the caller. The helper must detect
    that the child does not lead its group and fall back to the process.
    """
    proc = _spawn_sleeper()
    try:
        label = terminate_process_group(proc, grace=5.0)
        assert proc.poll() is not None, "process survived cancellation"
        if portability.HAS_KILLPG:
            # Group kill was available but correctly not used.
            assert label in ("terminate", "kill"), label
        else:
            assert label in ("terminate", "kill"), label
        # Reaching this line at all proves our own group survived.
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_terminate_process_group_falls_back_without_killpg(monkeypatch):
    """Simulated Windows: no killpg, no SIGKILL -> terminate()/kill()."""
    monkeypatch.setattr(portability, "HAS_KILLPG", False)
    monkeypatch.setattr(portability, "HAS_SIGKILL", False)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True)
    try:
        label = terminate_process_group(proc, grace=5.0)
        assert proc.poll() is not None, "process survived the fallback"
        assert label in ("terminate", "kill"), label
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_terminate_process_group_survives_an_already_dead_process(monkeypatch):
    monkeypatch.setattr(portability, "HAS_KILLPG", False)
    monkeypatch.setattr(portability, "HAS_SIGKILL", False)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    # Must not raise for a process that is already gone.
    assert terminate_process_group(proc, grace=1.0)


def test_capability_flags_match_this_interpreter():
    assert portability.HAS_SIGKILL == hasattr(portability.signal, "SIGKILL")
    assert portability.HAS_KILLPG == hasattr(os, "killpg")


# ---------------------------------------------------------------------------
# Static guard: nothing else may name a Windows-absent member
# ---------------------------------------------------------------------------

def _iter_forge_sources():
    for path in sorted((ROOT / "forge").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        if path.resolve() == HELPER.resolve():
            continue  # the one module allowed to know about this
        yield path


def test_no_windows_absent_signal_or_os_member_outside_the_helper():
    problems = []
    checked = 0
    for path in _iter_forge_sources():
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            for module, banned in WINDOWS_MISSING:
                if node.value.id == module and node.attr in banned:
                    problems.append(
                        "%s:%d: %s.%s does not exist on Windows - route it "
                        "through forge.core.portability"
                        % (path.relative_to(ROOT), node.lineno, module,
                           node.attr))
    assert checked > 200, "guard walked too few files: %d" % checked
    assert not problems, "\n".join(problems[:20])


def test_helper_itself_is_stdlib_only():
    """The desktop bundle depends on this module; it must stay stdlib-only.

    ``forge.desktop_app`` ships as a PyInstaller build for Python 3.8 /
    Windows 7 32-bit, and it reaches this module. An explicit expected set is
    used rather than ``sys.stdlib_module_names`` because that attribute is
    itself 3.9+.
    """
    tree = ast.parse(HELPER.read_text(encoding="utf-8"),
                     filename=str(HELPER))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "os", "signal", "subprocess"}, (
        "portability must import nothing but the stdlib trio, got: %s"
        % sorted(imported))
