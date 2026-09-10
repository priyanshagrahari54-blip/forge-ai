"""Build a standalone Forge Desktop executable with PyInstaller.

Prerequisites (on the machine that builds the distributable):

    pip install pyinstaller
    pip install -e .          # so the build sees the forge package

Usage:

    python scripts/build_desktop.py                 # one-folder build
    python scripts/build_desktop.py --onefile       # single .exe (Windows)
    python scripts/build_desktop.py --check         # print the command only

Notes:

* Build on the OS you ship for (a Windows .exe must be built on Windows).
* Tkinter support ships with PyInstaller; no extra hooks are needed.
* The app reads no data files at runtime: configuration comes from the
  project folders you open plus OLLAMA_URL / OLLAMA_MODEL / OPENAI_API_KEY.
* Python 3.11+ is required (there is no "Python 8": CPython versions go
  3.9, 3.10, 3.11, ...; Forge targets >=3.11 per pyproject.toml).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "launch_desktop.py"


def build_command(*, onefile: bool, name: str, windowed: bool) -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        "--paths", str(ROOT),
        "--collect-submodules", "forge",
        # Keep the stdlib GUI + sqlite + ssl pieces PyInstaller sometimes
        # prunes in aggressive builds.
        "--hidden-import", "tkinter",
        "--hidden-import", "sqlite3",
        "--hidden-import", "ssl",
        "--clean",
        "--noconfirm",
    ]
    if onefile:
        command.append("--onefile")
    if windowed:
        command.append("--windowed")  # no console window on Windows/macOS
    else:
        command.append("--console")
    command.append(str(ENTRY))
    return command


def main(argv: list[str] | None = None) -> int:
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
                            windowed=windowed)
    print("Build command:")
    print("  " + " ".join(command))
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
