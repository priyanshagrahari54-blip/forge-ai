"""QEMU / VM testing (A83): source → build → boot → judge from the console.

For an operating system or kernel, compiling proves almost nothing. The only
real evidence is that the artifact boots and says so. This module:

* launches QEMU with the recipe's argv (from a project profile — Forge's core
  knows nothing about ZEROOS);
* reads the **serial console**, which is the guest's own output;
* decides the outcome from markers the guest actually printed:
  ``ready_marker`` present and no ``failure_marker`` → ``booted``;
  ``failure_marker`` present → ``panic``; neither → ``no_marker``, which is a
  distinct verdict from a crash because the difference matters when debugging;
* records the console log the verdict came from, so a claim is checkable.

If QEMU is not installed the result is ``unavailable`` — never ``booted``, and
never silently skipped.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.builder.runner import CommandResult, CommandRunner

BOOTED = "booted"
PANIC = "panic"
NO_MARKER = "no_marker"
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
REFUSED = "refused"
NO_IMAGE = "no_image"
VERDICTS = (BOOTED, PANIC, NO_MARKER, TIMEOUT, UNAVAILABLE, REFUSED, NO_IMAGE)

MAX_CONSOLE_CHARS = 200_000
DEFAULT_BOOT_TIMEOUT = 120.0


@dataclass
class BootResult:
    """The measured outcome of one boot attempt."""

    verdict: str
    recipe: str = ""
    argv: Tuple[str, ...] = ()
    console: str = ""
    console_truncated: bool = False
    duration_ms: float = 0.0
    return_code: Optional[int] = None
    #: Which marker was found, and the line it was found on.
    ready_marker: str = ""
    failure_marker: str = ""
    ready_line: str = ""
    failure_line: str = ""
    reason: str = ""
    #: Mechanism used to stop the VM (SIGTERM/SIGKILL/…).
    cancellation: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == BOOTED

    def summary(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict, "ok": self.ok, "recipe": self.recipe,
            "argv": list(self.argv), "duration_ms": round(self.duration_ms, 1),
            "return_code": self.return_code,
            "console_chars": len(self.console),
            "console_truncated": self.console_truncated,
            "ready_marker": self.ready_marker,
            "failure_marker": self.failure_marker,
            "ready_line": self.ready_line,
            "failure_line": self.failure_line,
            "reason": self.reason, "cancellation": self.cancellation,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "console": self.console[-20_000:]}


def judge(console: str, *, ready_marker: str = "",
          failure_marker: str = "",
          timed_out: bool = False) -> Tuple[str, str, str]:
    """Decide a boot verdict from console text alone.

    Returns ``(verdict, ready_line, failure_line)``. The rules, in order:

    1. a failure marker wins — a panic that happened *after* the ready marker
       is still a panic, and reporting it as a successful boot would be the
       most damaging mistake this module could make;
    2. otherwise the ready marker means ``booted``;
    3. otherwise a timeout means ``timeout``;
    4. otherwise ``no_marker`` — the VM ran and printed things, but never said
       it was ready.
    """
    text = console or ""
    failure_line = _find_line(text, failure_marker)
    ready_line = _find_line(text, ready_marker)
    if failure_marker and failure_line:
        return PANIC, ready_line, failure_line
    if ready_marker and ready_line:
        return BOOTED, ready_line, failure_line
    if timed_out:
        return TIMEOUT, ready_line, failure_line
    return NO_MARKER, ready_line, failure_line


def _find_line(text: str, marker: str) -> str:
    if not marker:
        return ""
    needle = marker.strip()
    if not needle:
        return ""
    for line in text.splitlines():
        if needle in line:
            return line.strip()[:400]
    return ""


@dataclass
class BootScenario:
    """One boot test: which recipe, how long, what to look for."""

    name: str
    argv: Tuple[str, ...]
    ready_marker: str = ""
    failure_marker: str = ""
    timeout_seconds: float = DEFAULT_BOOT_TIMEOUT
    #: Repository-relative files that must exist before the boot is attempted.
    requires: Tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_recipe(cls, recipe: Any) -> "BootScenario":
        options = getattr(recipe, "options", {}) or {}
        return cls(
            name=getattr(recipe, "name", "boot"),
            argv=tuple(getattr(recipe, "argv", ())),
            ready_marker=str(options.get("ready_marker", "") or ""),
            failure_marker=str(options.get("failure_marker", "") or ""),
            timeout_seconds=float(getattr(recipe, "timeout_seconds",
                                          DEFAULT_BOOT_TIMEOUT)),
            requires=tuple(getattr(recipe, "evidence", ())),
            description=str(getattr(recipe, "description", "") or ""))

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "argv": list(self.argv),
                "ready_marker": self.ready_marker,
                "failure_marker": self.failure_marker,
                "timeout_seconds": self.timeout_seconds,
                "requires": list(self.requires),
                "description": self.description}


@dataclass
class BootReport:
    """Every boot attempt made for one artifact."""

    status: str
    attempts: List[BootResult] = field(default_factory=list)
    duration_ms: float = 0.0
    #: Build result that preceded the boot, when one was run.
    build: Dict[str, Any] = field(default_factory=dict)
    #: Scenarios that were skipped, with the reason.
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == BOOTED

    def summary(self) -> Dict[str, Any]:
        return {
            "status": self.status, "ok": self.ok,
            "attempts": [item.summary() for item in self.attempts],
            "skipped": list(self.skipped),
            "duration_ms": round(self.duration_ms, 1),
            "build": dict(self.build),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "consoles": {item.recipe: item.console[-8_000:]
                             for item in self.attempts}}


def render(report: BootReport) -> str:
    lines = ["vm boot: %s (%.0f ms)" % (report.status, report.duration_ms)]
    for item in report.attempts:
        lines.append("  %-16s %-12s %6.0f ms%s" % (
            item.recipe, item.verdict, item.duration_ms,
            " — %s" % item.reason if item.reason else ""))
        if item.ready_line:
            lines.append("      ready: %s" % item.ready_line)
        if item.failure_line:
            lines.append("      panic: %s" % item.failure_line)
    for item in report.skipped:
        lines.append("  %-16s skipped — %s" % (item.get("name", "?"),
                                               item.get("reason", "")))
    return "\n".join(lines)


class VmHarness:
    """Builds (optionally) and boots an artifact under QEMU."""

    def __init__(self, root: str | Path = ".", *, profile: Any = None,
                 runner: Optional[CommandRunner] = None) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.runner = runner or CommandRunner(self.root)

    # -- planning --------------------------------------------------------

    def scenarios(self) -> List[BootScenario]:
        """Boot scenarios from the active profile's boot recipes."""
        if self.profile is None:
            return []
        return [BootScenario.from_recipe(recipe)
                for recipe in self.profile.recipes_for("boot")]

    def available_scenarios(self) -> Tuple[List[BootScenario],
                                           List[Dict[str, Any]]]:
        """Split scenarios into runnable and skipped, with reasons."""
        runnable: List[BootScenario] = []
        skipped: List[Dict[str, Any]] = []
        for scenario in self.scenarios():
            missing = [item for item in scenario.requires
                       if not (self.root / item).exists()]
            if missing:
                skipped.append({"name": scenario.name,
                                "reason": "required artifact(s) missing: %s"
                                          % ", ".join(missing)})
                continue
            runnable.append(scenario)
        return runnable, skipped

    # -- running ---------------------------------------------------------

    def boot(self, scenario: BootScenario) -> BootResult:
        """Boot one scenario and judge it from the serial console."""
        started = time.monotonic()
        result = self.runner.run_cancel(
            list(scenario.argv), timeout=scenario.timeout_seconds)
        console = (result.stdout or "") + (result.stderr or "")
        if len(console) > MAX_CONSOLE_CHARS:
            console = console[-MAX_CONSOLE_CHARS:]
        if result.status == "unavailable":
            return BootResult(
                verdict=UNAVAILABLE, recipe=scenario.name,
                argv=scenario.argv, console=console,
                duration_ms=result.duration_ms, reason=result.reason)
        if result.status == "refused":
            return BootResult(
                verdict=REFUSED, recipe=scenario.name, argv=scenario.argv,
                console=console, duration_ms=result.duration_ms,
                reason=result.reason)
        # QEMU exits non-zero when the guest reboots or shuts down after a
        # panic; the console markers are the authority, and the exit code is
        # recorded alongside them.
        timed_out = result.cancellation in ("SIGTERM", "SIGKILL") or (
            result.duration_ms >= scenario.timeout_seconds * 1000.0)
        verdict, ready_line, failure_line = judge(
            console, ready_marker=scenario.ready_marker,
            failure_marker=scenario.failure_marker, timed_out=timed_out)
        reason = {
            BOOTED: "guest printed the ready marker on the serial console",
            PANIC: "guest printed the failure marker",
            TIMEOUT: "the guest never finished booting within %.0fs"
                     % scenario.timeout_seconds,
            NO_MARKER: ("the guest produced console output but never printed "
                        "a readiness or failure marker"),
        }.get(verdict, "")
        return BootResult(
            verdict=verdict, recipe=scenario.name, argv=scenario.argv,
            console=console,
            console_truncated=result.output_truncated,
            duration_ms=result.duration_ms, return_code=result.return_code,
            ready_marker=scenario.ready_marker,
            failure_marker=scenario.failure_marker,
            ready_line=ready_line, failure_line=failure_line,
            reason=reason, cancellation=result.cancellation)

    def run(self, *, build: bool = True,
            scenarios: Sequence[BootScenario] = ()) -> BootReport:
        """Build (optionally), then boot every applicable scenario."""
        started = time.monotonic()
        report = BootReport(status=UNAVAILABLE)
        wanted = list(scenarios) if scenarios else None
        runnable, skipped = self.available_scenarios()
        if wanted is not None:
            names = {item.name for item in wanted}
            runnable = [item for item in runnable if item.name in names]
        report.skipped = skipped

        if build and self.profile is not None:
            from forge.builder.engine import BuildEngine
            build_report = BuildEngine(self.root, profile=self.profile).build()
            report.build = build_report.summary()
            if not build_report.ok:
                report.status = NO_IMAGE
                report.duration_ms = (time.monotonic() - started) * 1000
                return report

        if not runnable:
            report.status = NO_IMAGE
            report.duration_ms = (time.monotonic() - started) * 1000
            return report

        verdicts = []
        for scenario in runnable:
            attempt = self.boot(scenario)
            report.attempts.append(attempt)
            verdicts.append(attempt.verdict)
            if attempt.verdict == PANIC:
                break

        if PANIC in verdicts:
            report.status = PANIC
        elif verdicts and all(item == BOOTED for item in verdicts):
            report.status = BOOTED
        elif TIMEOUT in verdicts:
            report.status = TIMEOUT
        elif UNAVAILABLE in verdicts and BOOTED not in verdicts:
            report.status = UNAVAILABLE
        else:
            report.status = NO_MARKER
        report.duration_ms = (time.monotonic() - started) * 1000
        return report


class BootTestAgent:
    """Adapter exposing the VM harness as a Forge agent executor."""

    __test__ = False
    name = "devops"
    role = "devops"

    def __init__(self, root: str | Path = ".", *, profile: Any = None,
                 build: bool = True) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.build_first = build
        self.harness = VmHarness(self.root, profile=profile)

    def describe(self) -> str:
        return ("Boots the built artifact under QEMU and judges the boot from "
                "the serial console.")

    def attach(self, context: Any) -> "BootTestAgent":
        self.context = context
        return self

    def execute(self, request: Any) -> Any:
        from forge.agents.execution import AgentResponse
        metadata = request.metadata or {}
        report = self.harness.run(
            build=bool(metadata.get("build", self.build_first)))
        context = getattr(self, "context", None)
        if context is not None:
            context.record("boot", "boot", report.summary(),
                           source="agent:devops",
                           note="judged from the serial console")
            context.note_turn(self.name, self.role,
                              "vm boot %s" % report.status,
                              ok=report.ok, verdict=report.status)
        return AgentResponse(
            success=report.ok, output=render(report), agent=self.name,
            stage=request.stage,
            error="" if report.ok else report.status,
            context_fingerprint=(request.context.fingerprint
                                 if request.context else ""),
            metadata={"verdict": report.status,
                      "attempts": str(len(report.attempts))})
