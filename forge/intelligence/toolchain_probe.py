"""Evidence-based toolchain probes for Forge language support.

A source file is not proof that a compiler or test runner is installed.
Only fixed, allow-listed version probes are executed; no project-provided command
or source file is run by this module.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ToolProbe:
    name: str
    executable: str
    version_args: Tuple[str, ...] = ("--version",)
    ecosystems: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolStatus:
    name: str
    executable: str
    available: bool
    path: str = ""
    version: str = ""
    version_checked: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "executable": self.executable,
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "version_checked": self.version_checked,
            "error": self.error,
        }


# This list intentionally probes tool availability, not whether a repository
# can be built. Actual builds/tests remain the responsibility of BuildEngine
# and TestEngine after project evidence and execution policy are checked.
DEFAULT_PROBES: Tuple[ToolProbe, ...] = (
    ToolProbe("Python", "python", ("--version",), ("python",)),
    ToolProbe("Python 3", "python3", ("--version",), ("python",)),
    ToolProbe("Node.js", "node", ("--version",), ("javascript", "typescript", "node")),
    ToolProbe("npm", "npm", ("--version",), ("javascript", "typescript", "node")),
    ToolProbe("Deno", "deno", ("--version",), ("deno",)),
    ToolProbe("Bun", "bun", ("--version",), ("bun",)),
    ToolProbe("Rust compiler", "rustc", ("--version",), ("rust",)),
    ToolProbe("Cargo", "cargo", ("--version",), ("rust",)),
    ToolProbe("Go", "go", ("version",), ("go",)),
    ToolProbe("Java compiler", "javac", ("-version",), ("java", "jvm")),
    ToolProbe("Java runtime", "java", ("-version",), ("java", "jvm")),
    ToolProbe("Kotlin", "kotlinc", ("-version",), ("kotlin", "jvm")),
    ToolProbe("Scala", "scala", ("-version",), ("scala", "jvm")),
    ToolProbe("C compiler", "cc", ("--version",), ("c", "cpp", "native")),
    ToolProbe("C++ compiler", "c++", ("--version",), ("cpp", "native")),
    ToolProbe("GNU C compiler", "gcc", ("--version",), ("c", "cpp", "native")),
    ToolProbe("Clang", "clang", ("--version",), ("c", "cpp", "objective-c", "native")),
    ToolProbe("CMake", "cmake", ("--version",), ("cmake", "native")),
    ToolProbe("Make", "make", ("--version",), ("make", "native")),
    ToolProbe(".NET SDK", "dotnet", ("--version",), ("csharp", "fsharp", "visual-basic", "dotnet")),
    ToolProbe("PHP", "php", ("--version",), ("php",)),
    ToolProbe("Composer", "composer", ("--version",), ("php",)),
    ToolProbe("Ruby", "ruby", ("--version",), ("ruby",)),
    ToolProbe("Bundler", "bundle", ("--version",), ("ruby",)),
    ToolProbe("Swift", "swift", ("--version",), ("swift", "objective-c", "objective-cpp")),
    ToolProbe("Dart", "dart", ("--version",), ("dart",)),
    ToolProbe("Lua", "lua", ("-v",), ("lua",)),
    ToolProbe("Perl", "perl", ("--version",), ("perl",)),
    ToolProbe("R", "R", ("--version",), ("r",)),
    ToolProbe("Julia", "julia", ("--version",), ("julia",)),
    ToolProbe("Elixir", "elixir", ("--version",), ("elixir",)),
    ToolProbe("Erlang", "erl", ("-eval", "halt().", "-noshell"), ("erlang",)),
    ToolProbe("Haskell GHC", "ghc", ("--version",), ("haskell",)),
    ToolProbe("Clojure", "clojure", ("--version",), ("clojure", "clojurescript")),
    ToolProbe("Elm", "elm", ("--version",), ("elm",)),
    ToolProbe("Solidity", "solc", ("--version",), ("solidity",)),
    ToolProbe("Vyper", "vyper", ("--version",), ("vyper",)),
    ToolProbe("Terraform", "terraform", ("version",), ("hcl", "terraform")),
    ToolProbe("PowerShell", "pwsh", ("--version",), ("powershell",)),
    ToolProbe("SQL client", "sqlite3", ("--version",), ("sql", "sqlite")),
    ToolProbe("PHP Composer", "composer", ("--version",), ("php",)),
)


def probe_tool(probe: ToolProbe, timeout: float = 2.0) -> ToolStatus:
    """Check PATH and, when present, run a fixed version command safely."""
    path = shutil.which(probe.executable)
    if not path:
        return ToolStatus(
            name=probe.name,
            executable=probe.executable,
            available=False,
            error="not found on PATH",
        )
    try:
        result = subprocess.run(
            [path, *probe.version_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=max(0.1, min(float(timeout), 5.0)),
            check=False,
            shell=False,
        )
        first_line = next(
            (line.strip() for line in result.stdout.splitlines() if line.strip()),
            "",
        )[:300]
        return ToolStatus(
            name=probe.name,
            executable=probe.executable,
            available=True,
            path=path,
            version=first_line,
            version_checked=True,
            error="" if result.returncode == 0 else f"version command exited {result.returncode}",
        )
    except subprocess.TimeoutExpired:
        return ToolStatus(
            name=probe.name, executable=probe.executable, available=True,
            path=path, version_checked=False, error="version probe timed out",
        )
    except OSError as exc:
        return ToolStatus(
            name=probe.name, executable=probe.executable, available=True,
            path=path, version_checked=False, error=f"version probe failed: {exc}",
        )


def probe_toolchains(
    probes: Iterable[ToolProbe] = DEFAULT_PROBES,
    timeout: float = 2.0,
) -> Dict[str, object]:
    """Return a serializable inventory; absence is explicit, never a pass."""
    statuses = [probe_tool(probe, timeout=timeout) for probe in probes]
    return {
        "schema_version": 1,
        "status": "probed",
        "tool_count": len(statuses),
        "available_count": sum(item.available for item in statuses),
        "version_checked_count": sum(item.version_checked for item in statuses),
        "tools": [item.to_dict() for item in statuses],
        "note": "Tool presence does not prove project build or test success.",
    }
