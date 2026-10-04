"""Universal test strategy discovery.

Test execution is adapter-driven. Forge detects evidence for the repository's
test ecosystem and never reports success merely because no test framework was
found.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TestStrategy:
    name: str
    command: tuple[str, ...]
    evidence: tuple[str, ...]
    tool_available: bool
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "evidence": list(self.evidence),
            "tool_available": self.tool_available,
            "priority": self.priority,
        }


@dataclass
class TestPlan:
    strategies: list[TestStrategy]

    @property
    def primary(self) -> TestStrategy | None:
        return self.strategies[0] if self.strategies else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategies": [item.to_dict() for item in self.strategies],
            "primary": self.primary.to_dict() if self.primary else None,
        }


class UniversalTestEngine:
    """Detect test frameworks without executing untrusted code."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def _exists(self, *names: str) -> list[str]:
        return [name for name in names if (self.root / name).exists()]

    def discover(self) -> TestPlan:
        found: list[TestStrategy] = []
        py = self._exists("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
        if py and (self.root / "tests").is_dir():
            found.append(TestStrategy(
                "pytest", ("python", "-B", "-m", "pytest", "-q"),
                tuple(py + ["tests"]), shutil.which("python") is not None, 10))
        js = self._exists("package.json")
        if js:
            package = (self.root / "package.json").read_text(
                encoding="utf-8", errors="replace").lower()
            if '"test"' in package:
                found.append(TestStrategy(
                    "npm-test", ("npm", "test"), tuple(js),
                    shutil.which("npm") is not None, 20))
        if self._exists("Cargo.toml") and (self.root / "tests").is_dir():
            found.append(TestStrategy(
                "cargo-test", ("cargo", "test", "--locked"),
                ("Cargo.toml", "tests"), shutil.which("cargo") is not None, 30))
        if self._exists("pom.xml"):
            found.append(TestStrategy(
                "maven-test", ("mvn", "-B", "test"), ("pom.xml",),
                shutil.which("mvn") is not None, 40))
        if self._exists("build.gradle", "build.gradle.kts", "gradlew"):
            wrapper = "gradlew" if (self.root / "gradlew").exists() else "gradle"
            found.append(TestStrategy(
                "gradle-test", (wrapper, "test"),
                tuple(self._exists("build.gradle", "build.gradle.kts", "gradlew")),
                shutil.which(wrapper) is not None, 50))
        found.sort(key=lambda item: (item.priority, item.name))
        return TestPlan(found)


__all__ = ["TestPlan", "TestStrategy", "UniversalTestEngine"]
