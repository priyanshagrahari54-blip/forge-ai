"""Desktop bundle composition (scripts/build_desktop.py).

The important invariant is the *reason* the build excludes the API stack:
``forge.desktop_app`` must not reach fastapi / starlette / uvicorn /
pydantic. If it ever does, excluding them would ship a broken executable, so
that reachability is asserted here rather than assumed.

It matters most for Windows 7 32-bit: ``pydantic_core`` is a Rust extension
and Rust 1.78 raised the ``*-pc-windows-msvc`` targets (``i686`` included) to
require Windows 10, so that binary must stay out of the desktop bundle.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_desktop.py"

# Third-party modules that pull in the API server and, through it, the
# Rust-compiled pydantic_core binary.
API_STACK = {"fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core"}

# Known optional dependency: imported inside a try/except, never required.
OPTIONAL = {"modal"}

# Every third-party root this project can pull in, declared explicitly.
# ``sys.stdlib_module_names`` would be tidier but is 3.10+, and this suite
# has to stay importable on the 3.8 floor it is guarding.
THIRD_PARTY = API_STACK | OPTIONAL | {"yaml", "httpx", "pytest", "vermin"}

# What the desktop app is expected to reach, and nothing more.
EXPECTED_REACH = {"yaml"} | OPTIONAL


def _load_build_desktop():
    spec = importlib.util.spec_from_file_location("build_desktop", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_desktop = _load_build_desktop()


def seen_roots(entry: str) -> set:
    """Total modules reachable from `entry` - proves the walk is not empty."""
    return _reachable(entry)[1]


def _reachable(entry: str):
    seen, stack, third = set(), [entry], set()
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        top = mod.split(".")[0]
        if top != "forge":
            if top in THIRD_PARTY:
                third.add(top)
            continue
        path = _resolve(mod)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parts = mod.split(".")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                stack.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 0:
                    stack.append(node.module)
                else:
                    base = parts[:len(parts) - node.level] \
                        if node.level <= len(parts) else []
                    stack.append(".".join(base + [node.module])
                                 if base else node.module)
    return third, seen


def _resolve(mod: str):
    path = ROOT / (mod.replace(".", "/") + ".py")
    if path.exists():
        return path
    init = ROOT / mod.replace(".", "/") / "__init__.py"
    return init if init.exists() else None


def _reachable_third_party(entry: str) -> set:
    """Third-party import roots reachable from `entry`."""
    return _reachable(entry)[0]


def test_desktop_app_never_reaches_the_api_stack():
    """The exclusion in build_desktop.py is safe because of this."""
    for entry in ("forge.desktop_app.app", "forge.desktop_app.__main__",
                  "forge.desktop_app.backend"):
        reached = _reachable_third_party(entry)
        assert not (reached & API_STACK), (
            "%s reaches the API stack %s - excluding those modules from the "
            "desktop bundle would produce a broken executable"
            % (entry, sorted(reached & API_STACK)))
        # Anything third-party that IS reached must be expected: PyYAML
        # (config loading) and the optional, try-guarded modal backend.
        unexpected = reached - EXPECTED_REACH
        assert not unexpected, (
            "%s imports %s, which the desktop bundle does not provide"
            % (entry, sorted(unexpected)))
        # And it must still be a real walk, not an empty one.
        assert len(seen_roots(entry)) > 50


def test_default_build_excludes_the_api_stack():
    command = build_desktop.build_command(
        onefile=False, name="ForgeDesktop", windowed=False)
    excluded = {command[i + 1] for i, part in enumerate(command)
                if part == "--exclude-module"}
    assert API_STACK <= excluded, sorted(API_STACK - excluded)
    assert "modal" in excluded


def test_full_build_keeps_the_api_stack():
    command = build_desktop.build_command(
        onefile=False, name="ForgeDesktop", windowed=False, full=True)
    assert "--exclude-module" not in command


def test_excluded_modules_match_the_declared_constant():
    command = build_desktop.build_command(
        onefile=False, name="X", windowed=False)
    excluded = {command[i + 1] for i, part in enumerate(command)
                if part == "--exclude-module"}
    assert excluded == set(build_desktop.API_ONLY_MODULES)


@pytest.mark.parametrize("onefile,flag", [(True, "--onefile"),
                                          (False, "--console")])
def test_shape_flags(onefile, flag):
    command = build_desktop.build_command(
        onefile=onefile, name="ForgeDesktop", windowed=False)
    assert flag in command


def test_windowed_and_console_are_mutually_exclusive():
    windowed = build_desktop.build_command(
        onefile=False, name="X", windowed=True)
    assert "--windowed" in windowed and "--console" not in windowed
    console = build_desktop.build_command(
        onefile=False, name="X", windowed=False)
    assert "--console" in console and "--windowed" not in console


def test_stdlib_pieces_are_forced_in():
    command = build_desktop.build_command(
        onefile=False, name="X", windowed=False)
    hidden = {command[i + 1] for i, part in enumerate(command)
              if part == "--hidden-import"}
    assert {"tkinter", "sqlite3", "ssl"} <= hidden


def test_entry_point_is_the_desktop_launcher():
    command = build_desktop.build_command(
        onefile=False, name="X", windowed=False)
    assert command[-1].endswith("launch_desktop.py")
    assert build_desktop.ENTRY.exists()


def test_missing_pyinstaller_is_reported_not_crashed(monkeypatch, capsys):
    monkeypatch.setattr(build_desktop.shutil, "which", lambda _: None)
    assert build_desktop.main(["--check"]) == 2
    assert "pip install pyinstaller" in capsys.readouterr().err


def test_check_mode_prints_the_command_and_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(build_desktop.shutil, "which",
                        lambda _: "/usr/bin/pyinstaller")
    calls = []
    monkeypatch.setattr(build_desktop.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    assert build_desktop.main(["--check", "--onefile"]) == 0
    assert calls == [], "--check must not execute PyInstaller"
    out = capsys.readouterr().out
    assert "--onefile" in out
    assert "pydantic" in out  # the exclusion is announced


def test_module_docstring_states_the_real_floor():
    """Regression guard: this file claimed 'Python 3.11+ is required'."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Python 3.11+ is required" not in text
    assert "Python 3.8+" in text
