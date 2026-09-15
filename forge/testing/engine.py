"""Unified testing pipeline (A83).

    code → build → static analysis → unit → integration → system
         → regression → performance

Each stage runs a real command, is parsed into per-test outcomes by
:mod:`forge.testing.results`, and reports its own status. The pipeline's
central rule:

    **Compiling is not testing.** A report is only ``ok`` when every required
    stage ran, and at least one stage produced actual test outcomes. A
    pipeline where nothing collected any tests is reported as
    ``no_tests_collected``, never as a pass.

Stages are planned from evidence — a project profile's recipes first, then
generic detection — and every planned stage records why it applies, so a
skipped stage is a stated decision rather than a silent omission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.builder.detection import detect
from forge.builder.runner import CommandResult, CommandRunner
from forge.testing.results import (
    NO_TESTS,
    PASSED,
    TestCase,
    TestParse,
    parse_test_output,
)

STATIC_ANALYSIS = "static-analysis"
UNIT = "unit"
INTEGRATION = "integration"
SYSTEM = "system"
REGRESSION = "regression"
PERFORMANCE = "performance"
STAGES = (STATIC_ANALYSIS, UNIT, INTEGRATION, SYSTEM, REGRESSION, PERFORMANCE)
#: Canonical order; a stage may be skipped but never reordered silently.
STAGE_ORDER = STAGES

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NO_TESTS = "no_tests_collected"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TIMEOUT = "timeout"
STATUS_NOT_PLANNED = "not_planned"


@dataclass
class TestStage:
    """One stage of the testing pipeline."""

    name: str
    argv: Tuple[str, ...]
    parser: str = "auto"
    required: bool = True
    timeout: float = 1800.0
    evidence: Tuple[str, ...] = ()
    source: str = "detected"          # profile | detected | caller
    description: str = ""
    enabled: bool = True
    cwd: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "argv": list(self.argv), "parser": self.parser,
            "required": self.required, "timeout": self.timeout,
            "evidence": list(self.evidence), "source": self.source,
            "description": self.description, "enabled": self.enabled,
        }


@dataclass
class StageResult:
    """The measured outcome of one stage."""

    stage: str
    status: str
    parse: TestParse = field(default_factory=lambda: TestParse("generic", "unknown"))
    argv: Tuple[str, ...] = ()
    return_code: Optional[int] = None
    duration_ms: float = 0.0
    required: bool = True
    reason: str = ""
    log: str = ""
    #: Extra structured evidence: build diagnostics, timing, resource use.
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASSED

    def summary(self) -> Dict[str, Any]:
        return {
            "stage": self.stage, "status": self.status, "ok": self.ok,
            "required": self.required, "argv": list(self.argv),
            "return_code": self.return_code,
            "duration_ms": round(self.duration_ms, 1),
            "reason": self.reason, "tests": self.parse.summary(),
            "metrics": dict(self.metrics),
        }


@dataclass
class TestReport:
    """The outcome of a whole pipeline run."""

    stages: List[StageResult] = field(default_factory=list)
    duration_ms: float = 0.0
    #: Stages that were planned but not enabled.
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def required_results(self) -> List[StageResult]:
        return [item for item in self.stages if item.required]

    @property
    def total_cases(self) -> int:
        return sum(len(item.parse.cases) for item in self.stages)

    @property
    def collected_any_tests(self) -> bool:
        return any(item.parse.cases for item in self.stages)

    @property
    def failures(self) -> List[str]:
        out: List[str] = []
        for item in self.stages:
            out.extend(item.parse.failures)
        return out

    @property
    def ok(self) -> bool:
        """True only when every required stage passed *and* tests ran.

        A green build with no tests collected is not a green test run.
        """
        if not self.required_results:
            return False
        if not all(item.ok for item in self.required_results):
            return False
        return self.collected_any_tests

    def status(self) -> str:
        if any(item.status == STATUS_FAILED for item in self.required_results):
            return STATUS_FAILED
        if not self.required_results:
            return STATUS_NOT_PLANNED
        if not self.collected_any_tests:
            return STATUS_NO_TESTS
        return STATUS_PASSED

    def stage(self, name: str) -> Optional[StageResult]:
        for item in self.stages:
            if item.stage == name:
                return item
        return None

    def summary(self) -> Dict[str, Any]:
        counts = {outcome: 0 for outcome in
                  ("passed", "failed", "error", "skipped", "xfail", "xpass")}
        for item in self.stages:
            for key, value in item.parse.counts.items():
                counts[key] = counts.get(key, 0) + value
        return {
            "status": self.status(),
            "ok": self.ok,
            "stages": [item.summary() for item in self.stages],
            "skipped": list(self.skipped),
            "duration_ms": round(self.duration_ms, 1),
            "total_cases": self.total_cases,
            "counts": counts,
            "failures": list(self.failures)[:100],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "detail": [item.parse.to_dict() for item in self.stages]}


def render(report: TestReport) -> str:
    """Human-readable pipeline result — the text an agent receives."""
    lines = ["tests: %s (%d case(s) in %.0f ms)" % (
        report.status(), report.total_cases, report.duration_ms)]
    for item in report.stages:
        counts = item.parse.counts
        lines.append("  %-17s %-9s %d passed, %d failed%s" % (
            item.stage, item.status, counts.get("passed", 0),
            counts.get("failed", 0) + counts.get("error", 0),
            "" if item.required else " (advisory)"))
        for failure in item.parse.failures[:10]:
            lines.append("      FAIL %s" % failure)
        if item.reason:
            lines.append("      %s" % item.reason)
    return "\n".join(lines)


class TestEngine:
    """Plan and run the staged test pipeline for one project."""

    # Not a pytest test class; prevents collection warnings when imported.
    __test__ = False

    def __init__(self, root: str | Path = ".", *, profile=None,
                 runner: Optional[CommandRunner] = None,
                 default_timeout: float = 1800.0) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.runner = runner or CommandRunner(
            self.root, default_timeout=default_timeout)

    # -- planning --------------------------------------------------------

    def _profile_recipes(self, kind: str) -> Tuple[Any, ...]:
        if self.profile is None:
            return ()
        return tuple(self.profile.recipes_for(kind))

    def plan(self, *, extra: Sequence[TestStage] = ()) -> List[TestStage]:
        """Plan every stage from evidence, in canonical order."""
        planned: Dict[str, TestStage] = {}

        for recipe in self._profile_recipes("analyze"):
            planned.setdefault(STATIC_ANALYSIS, TestStage(
                name=STATIC_ANALYSIS, argv=recipe.argv, parser=recipe.parser,
                required=False, timeout=recipe.timeout_seconds,
                evidence=recipe.evidence, source="profile", cwd=recipe.cwd,
                description=recipe.description or "Profile static analysis"))

        for recipe in self._profile_recipes("test"):
            lowered = recipe.name.lower()
            stage = (INTEGRATION if any(word in lowered for word in
                                        ("integration", "e2e", "system"))
                     else UNIT)
            planned.setdefault(stage, TestStage(
                name=stage, argv=recipe.argv, parser=recipe.parser,
                required=True, timeout=recipe.timeout_seconds,
                evidence=recipe.evidence, source="profile", cwd=recipe.cwd,
                description=recipe.description or "Profile test recipe"))

        for recipe in self._profile_recipes("bench"):
            planned.setdefault(PERFORMANCE, TestStage(
                name=PERFORMANCE, argv=recipe.argv, parser=recipe.parser,
                required=False, timeout=recipe.timeout_seconds,
                evidence=recipe.evidence, source="profile", cwd=recipe.cwd,
                description=recipe.description or "Profile benchmark recipe"))

        if UNIT not in planned:
            planned[UNIT] = self._detected_unit_stage()
        if STATIC_ANALYSIS not in planned:
            detected = self._detected_analysis_stage()
            if detected is not None:
                planned[STATIC_ANALYSIS] = detected

        for stage in extra:
            planned[stage.name] = stage

        out: List[TestStage] = []
        for name in STAGE_ORDER:
            if name in planned:
                out.append(planned[name])
        return out

    def _detected_unit_stage(self) -> TestStage:
        """The default test stage: pytest, matching this repository's runner."""
        import sys
        evidence: Tuple[str, ...] = tuple(
            item for item in ("pyproject.toml", "pytest.ini", "tox.ini",
                              "tests", "setup.cfg")
            if (self.root / item).exists())
        return TestStage(
            name=UNIT,
            argv=(sys.executable, "-B", "-m", "pytest", "-v",
                  "-p", "no:cacheprovider"),
            parser="pytest", required=True, evidence=evidence,
            source="detected",
            description="Repository test suite")

    def _detected_analysis_stage(self) -> Optional[TestStage]:
        import sys
        if (self.root / "pyproject.toml").exists():
            return TestStage(
                name=STATIC_ANALYSIS,
                argv=(sys.executable, "-B", "-m", "compileall", "-q", "."),
                parser="python", required=False, source="detected",
                description="Byte-compile the tree (syntax gate)")
        for system in detect(self.root):
            if system.name == "cargo":
                return TestStage(
                    name=STATIC_ANALYSIS,
                    argv=("cargo", "clippy", "--locked", "--all-targets"),
                    parser="rust", required=False, source="detected",
                    evidence=system.evidence,
                    description="Rust lints")
        return None

    # -- running ---------------------------------------------------------

    def run(self, *, stages: Sequence[str] = (),
            extra: Sequence[TestStage] = (),
            regression_targets: Sequence[str] = ()) -> TestReport:
        """Run the planned stages in order and return a structured report."""
        import time
        started = time.monotonic()
        planned = list(extra) + self.plan(extra=())
        wanted = {name for name in stages}
        report = TestReport()
        for stage in planned:
            if wanted and stage.name not in wanted:
                continue
            if not stage.enabled:
                report.skipped.append({**stage.to_dict(),
                                       "reason": "stage disabled"})
                continue
            if stage.name == REGRESSION and not regression_targets:
                report.skipped.append({
                    **stage.to_dict(),
                    "reason": "no previously failing tests to re-run"})
                continue
            result = self._run_stage(
                stage, regression_targets=regression_targets)
            report.stages.append(result)
            if not result.ok and result.required:
                # Stop at the first required failure: later stages depend on
                # earlier ones, and running them would only add noise.
                break
        report.duration_ms = (time.monotonic() - started) * 1000
        return report

    def _run_stage(self, stage: TestStage, *,
                   regression_targets: Sequence[str] = ()) -> StageResult:
        argv = list(stage.argv)
        if stage.name == REGRESSION and regression_targets:
            argv = [*argv, *[str(target) for target in regression_targets]]
        result = self.runner.run(argv, cwd=stage.cwd, timeout=stage.timeout)
        parse = parse_test_output(result.output, stage.parser,
                                  return_code=result.return_code)
        status = _status_for(result, parse)
        reason = result.reason
        if status == STATUS_NO_TESTS and stage.required:
            reason = reason or "no tests were collected by this stage"
        return StageResult(
            stage=stage.name, status=status, parse=parse, argv=tuple(argv),
            return_code=result.return_code, duration_ms=result.duration_ms,
            required=stage.required, reason=reason, log=result.output[-20_000:],
            metrics={"output_truncated": result.output_truncated,
                     "executable": result.executable})


def _status_for(result: CommandResult, parse: TestParse) -> str:
    """Map a command result plus its parse onto a stage status."""
    if result.status == "unavailable":
        return STATUS_UNAVAILABLE
    if result.status == "timeout":
        return STATUS_TIMEOUT
    if result.status == "refused":
        return STATUS_SKIPPED
    if parse.status == NO_TESTS or (
            not parse.cases and "no tests ran" in (result.output or "").lower()):
        return STATUS_NO_TESTS
    if parse.cases:
        return STATUS_PASSED if parse.status == PASSED else STATUS_FAILED
    # Nothing parsed: fall back to the exit code, which is real evidence.
    return STATUS_PASSED if result.succeeded else STATUS_FAILED
