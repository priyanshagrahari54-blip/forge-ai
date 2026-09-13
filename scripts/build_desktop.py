"""Build a standalone Forge Desktop executable with PyInstaller.

Prerequisites (on the machine that builds the distributable):

    pip install pyinstaller
    pip install -e .          # so the build sees the forge package

Usage:

    python scripts/build_desktop.py                 # one-folder build
    python scripts/build_desktop.py --onefile       # single .exe (Windows)
    python scripts/build_desktop.py --check         # print the command only
    python scripts/build_desktop.py --full          # also bundle the API/CLI

Notes:

* Build on the OS you ship for (a Windows .exe must be built on Windows).
* Tkinter support ships with PyInstaller; no extra hooks are needed.
* The app reads no data files at runtime: configuration comes from the
  project folders you open plus OLLAMA_URL / OLLAMA_MODEL / OPENAI_API_KEY.

Python version
    Forge supports **Python 3.8+** — see ``requires-python`` in
    ``pyproject.toml``, the matrix in ``.github/workflows/ci.yml``, and
    ``docs/PYTHON38-WINDOWS7.md``. PyInstaller 6.x declares
    ``<3.16,>=3.8``, so 3.8 builds are supported.

Windows 7 32-bit
    CPython 3.8 is the last series that runs on Windows 7 and the last with
    32-bit installers, so the desktop target is 3.8/win32. Two things matter:

    1. ``pydantic-core`` is a Rust extension, and Rust 1.78 (2024-05-02)
       raised the ``*-pc-windows-msvc`` targets — ``i686`` included — to
       require Windows 10. Its cp38 win32 wheel is therefore not expected to
       load on Windows 7. The desktop app does not import pydantic (only
       ``forge.api`` does), so this build excludes it by default and the
       resulting bundle has no Windows 10 floor. ``--full`` puts it back for
       anyone who wants the API server in the same executable.
    2. PyInstaller states it "should work on Windows 7 or newer" but only
       officially supports Windows 8+. Test on the actual target.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "launch_desktop.py"

#: Modules the desktop app never imports. Bundling them would add the
#: Rust-compiled ``pydantic_core`` binary, which carries a Windows 10 floor
#: and so breaks the Windows 7 32-bit target for no benefit. ``--full``
#: drops these exclusions.
API_ONLY_MODULES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic_core",
    "modal",
)

#: Stdlib pieces PyInstaller sometimes prunes in aggressive builds.
HIDDEN_IMPORTS = ("tkinter", "sqlite3", "ssl")


def build_command(*, onefile: bool, name: str, windowed: bool,
                  full: bool = False) -> List[str]:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        "--paths", str(ROOT),
        "--collect-submodules", "forge",
    ]
    if not full:
        for module in API_ONLY_MODULES:
            command += ["--exclude-module", module]
    # Keep the stdlib GUI + sqlite + ssl pieces PyInstaller sometimes
    # prunes in aggressive builds.
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    command += ["--clean", "--noconfirm"]
    if onefile:
        command.append("--onefile")
    if windowed:
        command.append("--windowed")  # no console window on Windows/macOS
    else:
        command.append("--console")
    command.append(str(ENTRY))
    return command


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the Forge Desktop distributable.")
    parser.add_argument("--onefile", action="store_true",
                        help="Single-file executable instead of one-folder")
    parser.add_argument("--name", default="ForgeDesktop")
    parser.add_argument("--windowed", action="store_true",
                        default=(sys.platform in ("win32", "darwin")),
                        help="No console window (default on Windows/macOS)")
    parser.add_argument("--console", action="store_true",
                        help="Force a console window (shows logs)")
    parser.add_argument("--full", action="store_true",
                        help="Also bundle the API/CLI stack (fastapi, "
                             "uvicorn, pydantic). Not usable on Windows 7: "
                             "pydantic-core's Rust binary needs Windows 10.")
    parser.add_argument("--check", action="store_true",
                        help="Print the PyInstaller command without running")
    args = parser.parse_args(argv)

    if shutil.which("pyinstaller") is None:
        print("PyInstaller is not installed. Run: pip install pyinstaller",
              file=sys.stderr)
        return 2
    if not ENTRY.exists():
        print(f"Entry point missing: {ENTRY}", file=sys.stderr)
        return 2

    windowed = args.windowed and not args.console
    command = build_command(onefile=args.onefile, name=args.name,
                            windowed=windowed, full=args.full)
    print("Build command:")
    print("  " + " ".join(command))
    if not args.full:
        print("\nExcluding the API/CLI stack "
              f"({', '.join(API_ONLY_MODULES)}): the desktop app does not "
              "import it, and pydantic-core's Rust binary requires Windows "
              "10. Use --full to bundle it anyway.")
    if args.check:
        return 0
    print(f"\nWorking directory: {ROOT}")
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        return completed.returncode
    dist = ROOT / "dist" / args.name
    print(f"\nDone. Distributable: {dist}")
    print("Ship the whole folder (or the single file with --onefile).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
