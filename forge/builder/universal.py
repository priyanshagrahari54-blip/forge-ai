"""Universal build planning and execution evidence.

The Universal Build Engine is an adapter layer, not a replacement for mature
build systems. It detects repository evidence, selects an available toolchain,
optionally performs a declared configure step, executes the build only when
explicitly requested, and records artifacts/evidence. Unknown build systems
fail closed instead of being guessed.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.builder.detection import artifact_snapshot, detect, diff_artifacts

MAX_OUTPUT = 8000


@dataclass(frozen=True)
class BuildPlan:
    system: str
    command: tuple[str, ...]
    tool_available: bool
    evidence: tuple[str, ...]
    configure: tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "command": list(self.command),
            "tool_available": self.tool_available,
            "evidence": list(self.evidence),
            "configure": list(self.configure),
            "note": self.note,
        }


@dataclass
class BuildResult:
    planned: bool = False
    executed: bool = False
    passed: bool = False
    system: str = ""
    command: list[str] = field(default_factory=list)
    configure_command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    output: str = ""
    artifacts: dict[str, list[str]] = field(
        default_factory=lambda: {"created": [], "modified": []})
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned, "executed": self.executed,
            "passed": self.passed, "system": self.system,
            "command": list(self.command),
            "configure_command": list(self.configure_command),
            "exit_code": self.exit_code, "output": self.output,
            "artifacts": self.artifacts, "error": self.error,
        }


class UniversalBuildEngine:
    """Evidence-driven multi-language build adapter."""

    def __init__(self, root: str | Path = ".", timeout: float = 900.0) -> None:
        self.root = Path(root).resolve()
        self.timeout = max(1.0, float(timeout))

    def plans(self) -> list[BuildPlan]:
        plans: list[BuildPlan] = []
        for system in detect(self.root):
            configure = tuple(system.options.get("configure", ()))
            plans.append(BuildPlan(
                system=system.name,
                command=tuple(system.argv),
                tool_available=system.available,
                evidence=tuple(system.evidence),
                configure=configure,
                note=system.note,
            ))
        return plans

    def plan(self, preferred: str | None = None) -> BuildPlan | None:
        plans = self.plans()
        if preferred:
            for item in plans:
                if item.system == preferred:
                    return item
        return plans[0] if plans else None

    def build(self, *, preferred: str | None = None,
              execute: bool = False,
              approved: bool = False) -> BuildResult:
        plan = self.plan(preferred)
        if plan is None:
            return BuildResult(error="no evidence-backed build system detected")
        result = BuildResult(
            planned=True, system=plan.system,
            command=list(plan.command),
            configure_command=list(plan.configure),
        )
        if not plan.tool_available:
            result.error = "required build tool is not available: " + plan.system
            return result
        if not execute:
            result.error = "plan-only; execution was not requested"
            return result
        if not approved:
            result.error = "build execution requires explicit authorization"
            return result

        before = artifact_snapshot(self.root)
        try:
            if plan.configure:
                proc = subprocess.run(
                    list(plan.configure), cwd=self.root, text=True,
                    capture_output=True, timeout=self.timeout, check=False)
                result.output = ((proc.stdout or "") + (proc.stderr or ""))[-MAX_OUTPUT:]
                if proc.returncode != 0:
                    result.executed = True
                    result.exit_code = proc.returncode
                    result.error = "configure step failed"
                    return result
            proc = subprocess.run(
                list(plan.command), cwd=self.root, text=True,
                capture_output=True, timeout=self.timeout, check=False)
            result.executed = True
            result.exit_code = proc.returncode
            result.output = ((result.output + "\n") + (proc.stdout or "") +
                             (proc.stderr or ""))[-MAX_OUTPUT:]
            result.passed = proc.returncode == 0
            result.error = "" if result.passed else "build command failed"
        except subprocess.TimeoutExpired:
            result.executed = True
            result.error = f"build exceeded {self.timeout:.0f}s"
        except OSError as exc:
            result.executed = True
            result.error = f"build could not start: {exc}"
        finally:
            result.artifacts = diff_artifacts(before, artifact_snapshot(self.root))
        return result


__all__ = ["BuildPlan", "BuildResult", "UniversalBuildEngine"]
