"""Universal build orchestration (A83).

One entry point for building any project Forge is pointed at:

    requirement → detect build system → (profile recipe | detected recipe)
                → run with a real timeout → parse diagnostics
                → collect artifacts → structured BuildReport

The report is the contract the rest of the platform consumes. Its central
rule, which is also the rule the rest of Forge follows: **a build that was
not run is never reported as a success.** ``BuildReport.ok`` is true only when
a command actually executed, exited zero, and produced no error-level
diagnostics. A missing toolchain is ``unavailable``. A build that exits zero
but emits ``error:`` lines is ``failed`` — exit codes lie; diagnostics are
evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.builder.detection import (
    BuildSystem,
    artifact_snapshot,
    detect,
    diff_artifacts,
)
from forge.builder.diagnostics import ParseResult, parse_diagnostics
from forge.builder.runner import CommandError, CommandResult, CommandRunner

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_UNAVAILABLE = "unavailable"
STATUS_REFUSED = "refused"
STATUS_NO_BUILD_SYSTEM = "no_build_system"


@dataclass
class BuildReport:
    """The measured outcome of one build."""

    status: str
    system: str = ""
    argv: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    duration_ms: float = 0.0
    return_code: Optional[int] = None
    diagnostics: ParseResult = field(
        default_factory=lambda: ParseResult("generic"))
    artifacts: Dict[str, List[str]] = field(default_factory=dict)
    log: str = ""
    log_truncated: bool = False
    reason: str = ""
    tool_available: bool = False
    #: Every candidate build system that was considered, with evidence.
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    #: Where the recipe came from: ``profile`` or ``detected``.
    source: str = ""
    cancelled: str = ""

    @property
    def ok(self) -> bool:
        """True only for a build that ran, exited zero, and had no errors."""
        return (self.status == STATUS_SUCCEEDED
                and self.return_code == 0
                and not self.diagnostics.errors)

    @property
    def ran(self) -> bool:
        return self.status in (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_TIMEOUT)

    def summary(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "ran": self.ran,
            "system": self.system,
            "source": self.source,
            "argv": list(self.argv),
            "evidence": list(self.evidence),
            "duration_ms": round(self.duration_ms, 1),
            "return_code": self.return_code,
            "tool_available": self.tool_available,
            "reason": self.reason,
            "diagnostics": self.diagnostics.summary(),
            "artifacts_created": len(self.artifacts.get("created", [])),
            "artifacts_modified": len(self.artifacts.get("modified", [])),
            "candidates": self.candidates,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "diagnostics": [item.to_dict()
                            for item in self.diagnostics.diagnostics],
            "artifacts": {key: list(value)
                          for key, value in self.artifacts.items()},
            "log": self.log[-20_000:],
            "log_truncated": self.log_truncated,
        }


def _tool_present(name: str) -> bool:
    from forge.builder.runner import resolve_executable
    try:
        return bool(resolve_executable(name))
    except CommandError:
        return False


def render(report: BuildReport) -> str:
    """Human-readable build result — the text an agent receives."""
    lines = [
        "build: %s (%s, %s)" % (report.status, report.system or "none",
                                report.source or "n/a"),
        "command: %s" % " ".join(report.argv),
        "duration: %.0f ms" % report.duration_ms,
        "diagnostics: %d error(s), %d warning(s)" % (
            len(report.diagnostics.errors), len(report.diagnostics.warnings)),
    ]
    if report.reason:
        lines.append("reason: %s" % report.reason)
    for item in report.diagnostics.errors[:20]:
        lines.append("  %s:%s: %s" % (item.file or "<unknown>",
                                      item.line or "?", item.message))
    created = report.artifacts.get("created", [])
    if created:
        lines.append("artifacts: %s" % ", ".join(created[:10]))
    return "\n".join(lines)


class BuildEngine:
    """Detect, run, and report builds for one project."""

    def __init__(self, root: str | Path = ".", *, profile=None,
                 runner: Optional[CommandRunner] = None,
                 default_timeout: float = 900.0,
                 dry_run: bool = False) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.runner = runner or CommandRunner(
            self.root, default_timeout=default_timeout, dry_run=dry_run)

    # -- planning --------------------------------------------------------

    def candidates(self) -> List[BuildSystem]:
        """Every build system that applies to this repository."""
        return detect(self.root)

    def recipe_candidates(self) -> List[Dict[str, Any]]:
        """Candidate list for the report: profile recipes first, then detected.

        A project profile's ``build_recipes`` win over detection, because a
        project that declares how it builds knows better than a file-pattern
        heuristic. Both are listed so the choice is auditable.
        """
        out: List[Dict[str, Any]] = []
        for recipe in self._profile_recipes("build"):
            out.append({"source": "profile", "name": recipe.name,
                        "argv": list(recipe.argv),
                        "evidence": list(recipe.evidence),
                        "parser": recipe.parser,
                        "cwd": recipe.cwd,
                        "tool": Path(recipe.argv[0]).name,
                        "tool_available": _tool_present(recipe.argv[0]),
                        "applies": self._recipe_applies(recipe)})
        for system in self.candidates():
            out.append({"source": "detected", "name": system.name,
                        "argv": list(system.argv),
                        "evidence": list(system.evidence),
                        "parser": system.parser,
                        "cwd": "",
                        "tool": system.tool,
                        "tool_available": system.available,
                        "applies": True, **system.options})
        return out

    def _profile_recipes(self, kind: str):
        if self.profile is None:
            return ()
        return tuple(self.profile.recipes_for(kind))

    def _recipe_applies(self, recipe) -> bool:
        """True when at least one of the recipe's evidence paths exists.

        A recipe with no declared evidence always applies — it is a project
        assertion, not a heuristic.
        """
        if not recipe.evidence:
            return True
        return any((self.root / item).exists() for item in recipe.evidence)

    def select(self, *, name: str = "") -> Optional[Dict[str, Any]]:
        """Choose the build recipe to run.

        ``name`` selects a specific recipe or system. Without it: the first
        applicable profile recipe wins, otherwise the most specific detected
        system.
        """
        recipes = list(self.recipe_candidates())
        if name:
            for entry in recipes:
                if entry["name"] == name:
                    return entry
            return None
        for entry in recipes:
            if entry["source"] == "profile" and entry["applies"]:
                return entry
        for entry in recipes:
            if entry["source"] == "detected":
                return entry
        return None

    # -- building --------------------------------------------------------

    def build(self, *, name: str = "", timeout: Optional[float] = None,
              target: str = "") -> BuildReport:
        """Build the project and return a structured report."""
        candidates = self.recipe_candidates()
        selected = self.select(name=name)
        if selected is None:
            if candidates:
                return BuildReport(
                    status=STATUS_REFUSED, candidates=candidates,
                    reason="no build recipe named %r" % name)
            return BuildReport(
                status=STATUS_NO_BUILD_SYSTEM, candidates=candidates,
                reason="no build system detected in %s" % self.root)

        argv = list(selected["argv"])
        if target:
            argv.append(target)
        report = BuildReport(
            status=STATUS_REFUSED, system=selected["name"], argv=tuple(argv),
            evidence=tuple(selected.get("evidence") or ()),
            source=selected["source"], candidates=candidates,
            tool_available=bool(selected.get("tool_available")))

        if not selected.get("tool_available"):
            report.status = STATUS_UNAVAILABLE
            report.reason = ("toolchain not installed: %s" % selected["tool"])
            return report

        before = artifact_snapshot(self.root)
        configure = selected.get("configure")
        if configure:
            configure_result = self.runner.run(
                list(configure), timeout=timeout)
            if not configure_result.succeeded:
                report.status = (
                    STATUS_UNAVAILABLE
                    if configure_result.status == STATUS_UNAVAILABLE
                    else STATUS_FAILED)
                report.reason = "configure step failed"
                report.log = configure_result.output[-20_000:]
                report.diagnostics = parse_diagnostics(
                    configure_result.output, selected.get("parser", "auto"))
                report.duration_ms = configure_result.duration_ms
                return report

        result = self.runner.run(argv, cwd=selected.get("cwd", ""),
                                 timeout=timeout)
        after = artifact_snapshot(self.root)
        report.status = result.status
        report.return_code = result.return_code
        report.duration_ms = result.duration_ms
        report.log = result.output
        report.log_truncated = result.output_truncated
        report.reason = result.reason
        report.cancelled = result.cancellation
        report.artifacts = diff_artifacts(before, after)
        report.diagnostics = parse_diagnostics(
            result.output, selected.get("parser", "auto"))

        if result.succeeded and report.diagnostics.errors:
            # Exit code 0 with error-level diagnostics: the build did not
            # really succeed, and saying so is the whole point of parsing.
            report.status = STATUS_FAILED
            report.reason = ("command exited 0 but reported %d error-level "
                             "diagnostics" % len(report.diagnostics.errors))
        return report


class BuildAgent:
    """Adapter exposing the build engine as a Forge agent executor."""

    __test__ = False
    name = "build"

    def __init__(self, root: str | Path = ".", *, profile=None,
                 default_timeout: float = 900.0) -> None:
        self.engine = BuildEngine(root, profile=profile,
                                  default_timeout=default_timeout)

    def describe(self) -> str:
        return ("Detects the project's build system and builds it, capturing "
                "compiler errors, warnings, artifacts, and duration.")

    def execute(self, request) -> Any:
        from forge.agents.execution import AgentResponse
        metadata = request.metadata or {}
        report = self.engine.build(
            name=str(metadata.get("build_system", "") or ""),
            target=str(metadata.get("target", "") or ""))
        text = render(report)
        return AgentResponse(
            success=report.ok, output=text, agent=self.name,
            stage=request.stage,
            error="" if report.ok else (report.reason or report.status),
            context_fingerprint=(request.context.fingerprint
                                 if request.context else ""),
            metadata={"status": report.status, "system": report.system,
                      "errors": str(len(report.diagnostics.errors)),
                      "warnings": str(len(report.diagnostics.warnings)),
                      "duration_ms": str(int(report.duration_ms))})
