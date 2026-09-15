"""Universal build orchestration (A83).

Detect the build system from repository evidence, run it with a real timeout
and no shell, parse the tool's diagnostics, and report what actually happened.

The package is named ``builder`` rather than ``build`` deliberately: this
repository's ``.gitignore`` ignores ``build/``, which would silently exclude a
``forge/build/`` package from version control.
"""
from __future__ import annotations

from forge.builder.detection import (
    BuildSystem,
    artifact_snapshot,
    detect,
    detect_primary,
    diff_artifacts,
)
from forge.builder.diagnostics import (
    Diagnostic,
    ParseResult,
    detect_parser,
    parse_diagnostics,
)
from forge.builder.engine import BuildAgent, BuildEngine, BuildReport, render
from forge.builder.runner import (
    CommandError,
    CommandResult,
    CommandRunner,
    resolve_executable,
    validate_argv,
)

__all__ = [
    "BuildAgent", "BuildEngine", "BuildReport", "BuildSystem", "CommandError",
    "CommandResult", "CommandRunner", "Diagnostic", "ParseResult",
    "artifact_snapshot", "detect", "detect_parser", "detect_primary",
    "diff_artifacts", "parse_diagnostics", "render", "resolve_executable",
    "validate_argv",
]
