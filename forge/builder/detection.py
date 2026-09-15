"""Build system detection (A83): evidence first, then availability.

Detection never guesses from a project's name. A build system is proposed
only when a file that implies it exists, the proposal records *which* file,
and the proposal separately records whether the tool is actually installed —
so "this project builds with CMake" and "CMake is available here" are two
different, separately reportable facts.

Ordering is deterministic: more specific systems first (a kernel Makefile
before a generic one, Cargo before Make when both exist), then by name.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SKIP_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    ".forge",
})
MAX_FILES = 20_000
#: Output directories whose new files count as build artifacts.
ARTIFACT_DIRECTORIES = ("build", "dist", "target", "out", "bin", "obj",
                        "cmake-build-debug", "cmake-build-release")
MAX_ARTIFACTS = 500


@dataclass(frozen=True)
class BuildSystem:
    """One detected way to build this repository."""

    name: str
    #: Repository-relative files that make this system applicable.
    evidence: Tuple[str, ...]
    argv: Tuple[str, ...]
    #: Executable that must exist for this system to run.
    tool: str
    #: Diagnostics parser understood by :mod:`forge.builder.diagnostics`.
    parser: str = "generic"
    #: Sort key: lower is more specific and therefore preferred.
    priority: int = 50
    #: Free-text note carried into the report.
    note: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """Whether the required tool is on PATH (checked, not assumed)."""
        if self.tool in ("python", "python3"):
            import sys
            return bool(sys.executable)
        return shutil.which(self.tool) is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "evidence": list(self.evidence),
            "argv": list(self.argv),
            "tool": self.tool,
            "tool_available": self.available,
            "parser": self.parser,
            "priority": self.priority,
            "note": self.note,
            "options": dict(self.options),
        }


def _walk(root: Path) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames
                       if name not in SKIP_DIRECTORIES]
        for filename in filenames:
            rel = (current / filename).relative_to(root).as_posix()
            out.append(rel)
            if len(out) >= MAX_FILES:
                return out
    return out


def _has(files: Sequence[str], *names: str) -> List[str]:
    lowered = {item.lower(): item for item in files}
    return [lowered[name.lower()] for name in names if name.lower() in lowered]


def _has_suffix(files: Sequence[str], *suffixes: str,
                limit: int = 3) -> List[str]:
    return [item for item in files
            if item.lower().endswith(suffixes)][:limit]


def _is_kernel_project(files: Sequence[str], root: Path) -> bool:
    """Kernel/OS evidence: linker scripts plus freestanding sources."""
    names = {Path(item).name.lower() for item in files}
    linker = bool({"kernel.ld", "linker.ld", "link.ld"} & names) or any(
        item.lower().endswith(".ld") for item in files)
    boot = any(Path(item).name.lower() in ("boot.asm", "boot.s", "boot.S",
                                           "head.s", "startup.s",
                                           "multiboot.asm")
               for item in files)
    freestanding = any(
        item.lower().endswith((".c", ".s", ".asm")) for item in files)
    return freestanding and (linker or boot)


def detect(root: str | Path) -> List[BuildSystem]:
    """Return every applicable build system, most specific first."""
    base = Path(root).resolve()
    files = _walk(base)
    found: List[BuildSystem] = []

    def add(system: BuildSystem) -> None:
        if system.evidence:
            found.append(system)

    # -- kernel / freestanding ------------------------------------------
    if _is_kernel_project(files, base):
        make = _has(files, "Makefile", "makefile", "GNUmakefile")
        add(BuildSystem(
            name="kernel",
            evidence=tuple(make) or tuple(
                _has_suffix(files, ".ld", ".asm", ".s", limit=2)),
            argv=("make", "-j", "all"),
            tool="make", parser="gcc", priority=5,
            note="Freestanding/kernel project: build the kernel image, then "
                 "validate by booting rather than by compiling."))

    # -- compiled languages ---------------------------------------------
    cargo = _has(files, "Cargo.toml")
    add(BuildSystem(name="cargo", evidence=tuple(cargo),
                    argv=("cargo", "build", "--locked"), tool="cargo",
                    parser="rust", priority=10))
    cmake = _has(files, "CMakeLists.txt")
    add(BuildSystem(name="cmake", evidence=tuple(cmake),
                    argv=("cmake", "--build", "build", "--parallel"),
                    tool="cmake", parser="gcc", priority=15,
                    options={"configure": ("cmake", "-S", ".", "-B", "build")}))
    meson = _has(files, "meson.build")
    add(BuildSystem(name="meson", evidence=tuple(meson),
                    argv=("meson", "compile", "-C", "build"), tool="meson",
                    parser="gcc", priority=16,
                    options={"configure": ("meson", "setup", "build")}))
    make = _has(files, "Makefile", "makefile", "GNUmakefile")
    add(BuildSystem(name="make", evidence=tuple(make),
                    argv=("make", "-j"), tool="make", parser="gcc",
                    priority=20))

    # -- managed ecosystems ---------------------------------------------
    gradle = _has(files, "build.gradle", "build.gradle.kts",
                  "settings.gradle", "settings.gradle.kts")
    wrapper = _has(files, "gradlew", "gradlew.bat")
    add(BuildSystem(
        name="gradle", evidence=tuple(gradle),
        argv=(("gradlew" if wrapper else "gradle"), "build"),
        tool="gradlew" if wrapper else "gradle", parser="java", priority=25))
    maven = _has(files, "pom.xml")
    add(BuildSystem(name="maven", evidence=tuple(maven),
                    argv=("mvn", "-B", "package"), tool="mvn", parser="java",
                    priority=26))
    package_json = _has(files, "package.json")
    if package_json:
        lock = _has(files, "pnpm-lock.yaml", "yarn.lock", "package-lock.json")
        manager = ("pnpm" if "pnpm-lock.yaml" in lock
                   else "yarn" if "yarn.lock" in lock else "npm")
        add(BuildSystem(name="npm", evidence=tuple(package_json + lock),
                        argv=(manager, "run", "build"), tool=manager,
                        parser="tsc", priority=30))

    # -- python ----------------------------------------------------------
    pyproject = _has(files, "pyproject.toml", "setup.py", "setup.cfg")
    if pyproject:
        add(BuildSystem(
            name="python", evidence=tuple(pyproject),
            argv=("python", "-B", "-m", "compileall", "-q", "."),
            tool="python", parser="python", priority=40,
            note="compileall is a syntax gate, not a packaging build. Use "
                 "the packaging recipe for a distribution."))

    found.sort(key=lambda system: (system.priority, system.name))
    return found


def detect_primary(root: str | Path) -> Optional[BuildSystem]:
    """The most specific applicable build system, or None when there is none."""
    systems = detect(root)
    return systems[0] if systems else None


def artifact_snapshot(root: str | Path) -> Dict[str, float]:
    """Record mtime+size of files in known output directories.

    Compared after a build to report which artifacts the build actually
    produced. Only declared output directories are watched, so a build
    writing into the source tree does not look like it produced artifacts.
    """
    base = Path(root).resolve()
    snapshot: Dict[str, float] = {}
    for name in ARTIFACT_DIRECTORIES:
        directory = base / name
        if not directory.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(directory)):
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = path.relative_to(base).as_posix()
                snapshot[key] = float("%d.%d" % (stat.st_size,
                                                 int(stat.st_mtime_ns)))
                if len(snapshot) >= MAX_ARTIFACTS:
                    return snapshot
    return snapshot


def diff_artifacts(before: Dict[str, float],
                   after: Dict[str, float]) -> Dict[str, List[str]]:
    """Report created and modified artifacts between two snapshots."""
    created = sorted(key for key in after if key not in before)
    modified = sorted(key for key in after
                      if key in before and before[key] != after[key])
    return {"created": created[:MAX_ARTIFACTS],
            "modified": modified[:MAX_ARTIFACTS]}
