"""Windows 7 / py3.8 host guards for the Native AI Engine.

The production target is a Lenovo G560: Windows 7, 32-bit, Python 3.8, 2 GB
RAM. This suite keeps that promise statically and behaviorally:

* every native module must parse under the 3.8 grammar and use only 3.8
  typing constructs (the repo-wide ``test_python38_compat`` covers syntax;
  this file re-checks the native package explicitly with a native-only walk,
  so a regression here is pinpointed);
* no Win10/11-only or symlink/Unix-only APIs anywhere in the package;
* all path handling is separator-agnostic (posix + windows forms), Windows
  drive letters/backslashes are *rejected* at the change boundary, and the
  snapshot writer uses atomic replace semantics (no symlinks, no flock);
* no mandatory third-party dependencies: the native package imports only
  stdlib + Forge modules at module scope.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NATIVE_DIR = ROOT / "forge" / "native"

FORBIDDEN_IMPORTS = {
    # Unix-only or not-3.8-hostile APIs that would break Windows 7.
    "fcntl", "pwd", "grp", "termios", "pty", "resource", "nis",
    "ossaudiodev", "spwd",
    # Win10+/11-only or heavyweight runtime dependencies:
    "ctypes.windll",  # attribute use; guarded below
}

FORBIDDEN_MODULE_ATTRS = {
    "symlink_to", "readlink",  # symlink semantics are unreliable on Win7
    "os.setsid", "os.killpg",  # POSIX process groups
    "select.epoll", "asyncio.get_running_loop",  # 3.7+ ok, but loop policy
    # differs on Win7; the engine is synchronous by design
    "Path.walk",  # 3.12
}


def _native_files():
    return sorted(p for p in NATIVE_DIR.rglob("*.py")
                  if "__pycache__" not in str(p))


def test_native_package_is_reasonably_sized():
    files = _native_files()
    assert len(files) >= 12, "native package should hold the full A81 stack"


@pytest.mark.parametrize("path", _native_files(), ids=lambda p: p.name)
def test_native_file_parses_as_python38(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path),
                     feature_version=(3, 8))
    assert tree is not None


@pytest.mark.parametrize("path", _native_files(), ids=lambda p: p.name)
def test_native_file_avoids_win7_hostile_apis(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                assert head not in FORBIDDEN_IMPORTS, (
                    "%s imports %s (not available/appropriate on the "
                    "Windows 7 target)" % (path.name, alias.name))
        if isinstance(node, ast.ImportFrom) and node.module:
            head = node.module.split(".")[0]
            assert head not in FORBIDDEN_IMPORTS
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_MODULE_ATTRS:
                dotted = getattr(node.value, "id", "")
                # os.symlink is POSIX-flavored on Win7 without privileges;
                # never used by the native engine.
                pytest.fail("%s uses .%s (forbidden on the target; dotted=%s)"
                            % (path.name, node.attr, dotted))
    assert "ctypes.windll" not in source


#: Fallback stdlib names when sys.stdlib_module_names (3.10+) is absent.
_STDLIB_FALLBACK = {
    "argparse", "collections", "contextlib", "copy", "dataclasses", "enum",
    "hashlib", "heapq", "importlib", "inspect", "io", "json", "logging",
    "math", "os", "pathlib", "posixpath", "ntpath", "queue", "random", "re",
    "shutil", "stat", "string", "subprocess", "sys", "tempfile", "textwrap",
    "threading", "time", "traceback", "types", "typing", "unicodedata",
    "urllib", "uuid",
}


@pytest.mark.parametrize("path", _native_files(), ids=lambda p: p.name)
def test_native_modules_have_no_third_party_module_scope_imports(path):
    """Only stdlib + forge imports at module scope (fast cold start)."""
    import sys
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    stdlib = set(getattr(sys, "stdlib_module_names", ()) or
                 _STDLIB_FALLBACK)
    for node in ast.iter_child_nodes(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            if name == "forge":
                continue
            assert name in stdlib, (
                "%s imports third-party %r at module scope; the native "
                "engine must start on a bare py3.8 install"
                % (path.name, name))


def test_snapshot_write_is_atomic_replace_without_symlinks(tmp_path):
    from forge.native.state import NativeStatusSnapshot, read_snapshot, \
        write_snapshot
    first = NativeStatusSnapshot(engine_state="planning")
    path = write_snapshot(tmp_path, first)
    second = NativeStatusSnapshot(engine_state="verifying")
    write_snapshot(tmp_path, second)  # replace over an existing file
    assert read_snapshot(tmp_path)["engine_state"] == "verifying"
    assert not path.with_name(path.name + ".tmp").exists()
    assert not path.is_symlink()


def test_windows_style_paths_are_rejected_at_the_change_boundary(tmp_path):
    from helpers_native_ai import write_repo
    from forge.native.engine import NativeAIEngine
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    for evil in ("C:\\Windows\\evil.py", "..\\escape.py",
                 "subdir\\..\\..\\x.py", "/etc/passwd"):
        report = engine.coding.validate_changes({evil: "x = 1\n"})
        assert not report.ok, evil
        assert report.would_change == []


def test_windows_path_forms_stay_safe_in_declared_checks(tmp_path):
    from helpers_native_ai import write_repo
    from forge.native.verification import NativeVerifier
    write_repo(tmp_path)
    (tmp_path / "calc.py").write_text("x = 1\n", encoding="utf-8")
    # forward-slash relative paths work; backslash forms must not smuggle
    # traversal past the verifier:
    ok = NativeVerifier(tmp_path).diff_validation(["calc.py"], "x = 1")
    assert ok.passed
    bad = NativeVerifier(tmp_path).diff_validation(
        ["..\\other.py"], "x = 1")
    assert not bad.passed


def test_engine_runs_under_windows_path_semantics(tmp_path, monkeypatch):
    """ntpath-shaped inputs must not crash planning or context building."""
    from helpers_native_ai import write_repo
    from forge.native.engine import NativeAIEngine
    write_repo(tmp_path)
    plan = NativeAIEngine(tmp_path, persist_status=False).run_probe_plan(
        r"fix calc.py and the file docs\readme.md")
    assert plan["ok"]
    # backslash-containing tokens simply don't match repo files (no crash):
    assert plan["steps"] >= 3


def test_no_shell_expansion_in_test_runner_path(tmp_path):
    """The pytest command is argv-list based (Windows 7 has no sh)."""
    import sys
    from helpers_native_ai import write_repo
    from forge.native.engine import NativeAIEngine
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    command, accepted = engine.coding.test_command(["test_calc.py"])
    assert command[0] == sys.executable
    assert all(isinstance(arg, str) for arg in command)  # no shell string
    assert accepted == ["test_calc.py"]
