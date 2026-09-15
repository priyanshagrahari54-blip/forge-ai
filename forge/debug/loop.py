"""Automated debugging loop (A83).

    failure → collect logs → diagnose → inspect source → propose fix
            → apply fix → rebuild → re-run the failing test → regression run

The loop is bounded and honest:

* **Iteration cap.** ``max_iterations`` (1-5) is enforced; the loop always
  stops and always reports that it stopped.
* **No-op detection.** Each failure has a fingerprint (category + suspects +
  failing tests). A fix that leaves the fingerprint unchanged ends the loop:
  repeating the same attempt is waste, not persistence.
* **Every fix is recorded.** Each attempt — including the ones that made
  things worse — lands in the ledger with its diagnosis, the files it
  touched, and the measured results. Nothing is edited silently.
* **Rollback is real.** Original file contents are held for the run; when the
  loop ends without a fix and ``rollback_on_failure`` is set, the touched
  files are restored and the restoration is recorded.
* **A green build is not a green test.** The loop only reports ``fixed`` when
  the previously failing tests pass *and* the regression run passes.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.debug.diagnosis import (
    Diagnosis,
    diagnose_build_failure,
    diagnose_test_failure,
)

FIXED = "fixed"
STILL_FAILING = "still_failing"
REGRESSION = "regression"
UNCHANGED = "unchanged"
NO_FIX_AVAILABLE = "no_fix_available"
BUDGET_EXHAUSTED = "budget_exhausted"
NO_FAILURE = "no_failure"
REFUSED = "refused"
OUTCOMES = (FIXED, STILL_FAILING, REGRESSION, UNCHANGED, NO_FIX_AVAILABLE,
            BUDGET_EXHAUSTED, NO_FAILURE, REFUSED)

MAX_ITERATIONS = 5
MAX_SUSPECT_FILES = 6
MAX_SUSPECT_BYTES = 24_000
MAX_LEDGER_ENTRIES = 200
LEDGER_PATH = ".forge/debug/ledger.json"


@dataclass
class FailureContext:
    """What a fix strategy is given: the failure and the relevant source."""

    kind: str                                    # "test" | "build"
    diagnosis: Diagnosis
    #: Repository-relative path -> current content, for suspect files only.
    sources: Dict[str, str] = field(default_factory=dict)
    #: Earlier attempts in this loop, so a strategy can avoid repeating one.
    attempts: Tuple["DebugAttempt", ...] = ()
    log: str = ""

    def source(self, path: str) -> str:
        return self.sources.get(path, "")


@dataclass
class DebugAttempt:
    """One bounded repair attempt, fully recorded."""

    iteration: int
    diagnosis: Diagnosis
    proposed: List[Dict[str, Any]] = field(default_factory=list)
    applied: List[str] = field(default_factory=list)
    build: Dict[str, Any] = field(default_factory=dict)
    rerun: Dict[str, Any] = field(default_factory=dict)
    regression: Dict[str, Any] = field(default_factory=dict)
    outcome: str = STILL_FAILING
    reason: str = ""
    strategy: str = ""
    duration_ms: float = 0.0
    rolled_back: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "diagnosis": self.diagnosis.to_dict(),
            "proposed": list(self.proposed),
            "applied": list(self.applied),
            "build": dict(self.build),
            "rerun": dict(self.rerun),
            "regression": dict(self.regression),
            "outcome": self.outcome,
            "reason": self.reason,
            "strategy": self.strategy,
            "duration_ms": round(self.duration_ms, 1),
            "rolled_back": self.rolled_back,
        }


@dataclass
class DebugReport:
    """The outcome of a whole debug loop."""

    outcome: str
    iterations: int
    max_iterations: int
    attempts: List[DebugAttempt] = field(default_factory=list)
    duration_ms: float = 0.0
    rolled_back: bool = False
    #: The failures that remain, if any.
    remaining_failures: List[str] = field(default_factory=list)

    @property
    def fixed(self) -> bool:
        return self.outcome == FIXED

    def summary(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "fixed": self.fixed,
            "iterations": self.iterations,
            "max_iterations": self.max_iterations,
            "duration_ms": round(self.duration_ms, 1),
            "rolled_back": self.rolled_back,
            "remaining_failures": list(self.remaining_failures)[:50],
            "attempts": [
                {"iteration": item.iteration, "outcome": item.outcome,
                 "category": item.diagnosis.category,
                 "applied": list(item.applied), "reason": item.reason}
                for item in self.attempts
            ],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "attempts": [item.to_dict() for item in self.attempts]}


class FixStrategy:
    """Proposes changes for a diagnosed failure. Override ``propose``."""

    name = "strategy"

    def propose(self, context: FailureContext) -> List[Dict[str, str]]:
        """Return ``[{"path", "content"}]`` edits, or ``[]`` for "no idea".

        Returning an empty list is a legitimate answer: it means the strategy
        has no fix to offer, and the loop records that honestly instead of
        inventing an edit.
        """
        return []


class CallableFixStrategy(FixStrategy):
    """Adapts a callable into a fix strategy."""

    def __init__(self, name: str, worker: Callable[[FailureContext], Any]) -> None:
        self.name = name
        self.worker = worker

    def propose(self, context: FailureContext) -> List[Dict[str, str]]:
        result = self.worker(context) or []
        return [dict(item) for item in result if isinstance(item, dict)]


class FixLedger:
    """Append-only record of every automated fix Forge has attempted."""

    def __init__(self, root: str | Path = ".", *,
                 path: str = LEDGER_PATH,
                 max_entries: int = MAX_LEDGER_ENTRIES) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / path
        self.max_entries = max(1, max_entries)

    def read(self) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def append(self, entries: Sequence[Dict[str, Any]]) -> int:
        if not entries:
            return 0
        combined = self.read() + [dict(item) for item in entries]
        combined = combined[-self.max_entries:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(combined, indent=2),
                                 encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return 0
        return len(entries)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.read()[-max(1, limit):]

    def repeated_failures(self) -> Dict[str, int]:
        """How often each fix has already been attempted, by fingerprint.

        This is how Forge avoids retrying an approach it has already seen
        fail — the same question long-term memory answers at project scope.
        """
        counts: Dict[str, int] = {}
        for entry in self.read():
            key = str(entry.get("fingerprint", ""))
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts


class AutomatedDebugLoop:
    """Bounded, recorded failure → fix → verify loop."""

    def __init__(self, root: str | Path = ".", *,
                 tester,
                 strategy: Optional[FixStrategy] = None,
                 builder=None,
                 ledger: Optional[FixLedger] = None,
                 max_iterations: int = 3,
                 regression_targets: Sequence[str] = (),
                 rollback_on_failure: bool = True,
                 writer: Optional[Callable[[str, str], None]] = None) -> None:
        if not 1 <= int(max_iterations) <= MAX_ITERATIONS:
            raise ValueError(
                "max_iterations must be within [1, %d]" % MAX_ITERATIONS)
        self.root = Path(root).resolve()
        self.tester = tester
        self.builder = builder
        self.strategy = strategy or FixStrategy()
        self.ledger = ledger if ledger is not None else FixLedger(self.root)
        self.max_iterations = int(max_iterations)
        self.regression_targets = tuple(regression_targets)
        self.rollback_on_failure = rollback_on_failure
        self._writer = writer

    # -- loop ------------------------------------------------------------

    def run(self) -> DebugReport:
        started = time.monotonic()
        attempts: List[DebugAttempt] = []
        originals: Dict[str, str] = {}
        last_fingerprint = ""
        remaining: List[str] = []
        outcome = NO_FAILURE

        baseline = self._test()
        if baseline["ok"]:
            return DebugReport(
                outcome=NO_FAILURE, iterations=0,
                max_iterations=self.max_iterations,
                duration_ms=(time.monotonic() - started) * 1000)
        remaining = list(baseline.get("failures", []))

        for iteration in range(1, self.max_iterations + 1):
            attempt_started = time.monotonic()
            diagnosis = diagnose_test_failure(
                baseline.get("parse"), baseline.get("log", ""), root=self.root)
            if not diagnosis.failing_tests:
                diagnosis.failing_tests = list(remaining)

            fingerprint = diagnosis.fingerprint()
            if fingerprint == last_fingerprint and attempts:
                attempts.append(DebugAttempt(
                    iteration=iteration, diagnosis=diagnosis,
                    outcome=UNCHANGED, strategy=self.strategy.name,
                    reason="the previous fix did not change the failure; "
                           "stopping instead of repeating the same attempt",
                    duration_ms=(time.monotonic() - attempt_started) * 1000))
                outcome = UNCHANGED
                break
            last_fingerprint = fingerprint

            sources = self._collect_sources(diagnosis)
            context = FailureContext(
                kind="test", diagnosis=diagnosis, sources=sources,
                attempts=tuple(attempts), log=baseline.get("log", ""))
            proposed = self._safe_propose(context)
            attempt = DebugAttempt(
                iteration=iteration, diagnosis=diagnosis,
                proposed=proposed, strategy=self.strategy.name)

            if not proposed:
                attempt.outcome = NO_FIX_AVAILABLE
                attempt.reason = ("the %s strategy proposed no change"
                                  % self.strategy.name)
                attempt.duration_ms = (time.monotonic() - attempt_started) * 1000
                attempts.append(self._record(attempt, fingerprint))
                outcome = NO_FIX_AVAILABLE
                break

            applied, backed_up = self._apply(proposed, originals)
            attempt.applied = applied
            if not applied:
                attempt.outcome = REFUSED
                attempt.reason = "no proposed change could be applied"
                attempt.duration_ms = (time.monotonic() - attempt_started) * 1000
                attempts.append(self._record(attempt, fingerprint))
                outcome = REFUSED
                break

            if self.builder is not None:
                build = self.builder.build()
                attempt.build = build.summary()
                if not build.ok:
                    attempt.outcome = STILL_FAILING
                    attempt.reason = "the build failed after the fix"
                    attempt.duration_ms = (
                        time.monotonic() - attempt_started) * 1000
                    attempts.append(self._record(attempt, fingerprint))
                    baseline = self._test()
                    remaining = list(baseline.get("failures", []))
                    continue

            rerun = self._test(targets=diagnosis.failing_tests)
            attempt.rerun = {
                "ok": rerun["ok"], "failures": rerun.get("failures", []),
                "cases": rerun.get("cases", 0)}
            if rerun["ok"]:
                regression = self._test(
                    targets=self.regression_targets or None)
                attempt.regression = {
                    "ok": regression["ok"],
                    "failures": regression.get("failures", []),
                    "cases": regression.get("cases", 0)}
                if regression["ok"]:
                    attempt.outcome = FIXED
                    attempt.duration_ms = (
                        time.monotonic() - attempt_started) * 1000
                    attempts.append(self._record(attempt, fingerprint))
                    return DebugReport(
                        outcome=FIXED, iterations=len(attempts),
                        max_iterations=self.max_iterations, attempts=attempts,
                        duration_ms=(time.monotonic() - started) * 1000)
                attempt.outcome = REGRESSION
                attempt.reason = ("the failing tests pass but the regression "
                                  "run does not")
                remaining = list(regression.get("failures", []))
            else:
                attempt.outcome = STILL_FAILING
                attempt.reason = "the failing tests still fail after the fix"
                remaining = list(rerun.get("failures", []))
            attempt.duration_ms = (time.monotonic() - attempt_started) * 1000
            attempts.append(self._record(attempt, fingerprint))
            baseline = self._test()
            outcome = attempt.outcome

        else:
            outcome = BUDGET_EXHAUSTED if attempts and attempts[
                -1].outcome not in (FIXED,) else outcome

        rolled_back = False
        if self.rollback_on_failure and originals and outcome != FIXED:
            rolled_back = self._restore(originals)
            for attempt in attempts:
                attempt.rolled_back = rolled_back
        return DebugReport(
            outcome=outcome, iterations=len(attempts),
            max_iterations=self.max_iterations, attempts=attempts,
            duration_ms=(time.monotonic() - started) * 1000,
            rolled_back=rolled_back, remaining_failures=remaining)

    # -- helpers ---------------------------------------------------------

    def _safe_propose(self, context: FailureContext) -> List[Dict[str, str]]:
        try:
            proposed = self.strategy.propose(context) or []
        except Exception as exc:  # noqa: BLE001 - a strategy must not kill the loop
            return []
        cleaned: List[Dict[str, str]] = []
        for item in proposed:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            content = item.get("content")
            if not path or not isinstance(content, str):
                continue
            cleaned.append({"path": path, "content": content})
        return cleaned[:MAX_SUSPECT_FILES]

    def _collect_sources(self, diagnosis: Diagnosis) -> Dict[str, str]:
        sources: Dict[str, str] = {}
        for candidate in diagnosis.suspect_files[:MAX_SUSPECT_FILES]:
            path = self.root / candidate
            try:
                resolved = path.resolve()
                resolved.relative_to(self.root)
                text = resolved.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            sources[candidate] = text[:MAX_SUSPECT_BYTES]
        return sources

    def _apply(self, proposed: Sequence[Dict[str, str]],
               originals: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
        applied: List[str] = []
        for item in proposed:
            candidate = item["path"].replace("\\", "/")
            path = (self.root / candidate).resolve()
            try:
                path.relative_to(self.root)
            except ValueError:
                continue
            if candidate not in originals:
                try:
                    originals[candidate] = (path.read_text(encoding="utf-8")
                                            if path.is_file() else "")
                except OSError:
                    originals[candidate] = ""
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._writer is not None:
                    self._writer(candidate, item["content"])
                else:
                    path.write_text(item["content"], encoding="utf-8")
            except OSError:
                continue
            applied.append(candidate)
        return applied, originals

    def _restore(self, originals: Dict[str, str]) -> bool:
        restored = True
        for candidate, content in originals.items():
            path = self.root / candidate
            try:
                if content:
                    path.write_text(content, encoding="utf-8")
                elif path.is_file():
                    path.unlink()
            except OSError:
                restored = False
        return restored

    def _test(self, targets: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Run the tester and normalise whatever it returns.

        Accepts a :class:`~forge.testing.engine.TestReport`, an
        ``AgentResponse``-like object, or a plain mapping, so the loop works
        with the real engines and with a test double alike.
        """
        result = self.tester(targets=list(targets)) if targets else self.tester()
        return _normalise_test_result(result)

    def _record(self, attempt: DebugAttempt, fingerprint: str) -> DebugAttempt:
        self.ledger.append([{
            "fingerprint": fingerprint,
            "at": time.time(),
            **attempt.to_dict(),
        }])
        return attempt


def _normalise_test_result(result: Any) -> Dict[str, Any]:
    """Reduce any tester return value to ``ok``/``failures``/``cases``/``log``."""
    if result is None:
        return {"ok": False, "failures": [], "cases": 0, "log": "",
                "parse": None}
    summary = getattr(result, "summary", None)
    if callable(summary):
        data = summary()
        parse = None
        stages = getattr(result, "stages", None)
        if stages:
            from forge.testing.results import TestParse
            combined = TestParse("combined", data.get("status", "unknown"))
            for stage in stages:
                combined.cases.extend(stage.parse.cases)
                combined.failures.extend(stage.parse.failures)
                combined.reported = data.get("counts", {})
            combined.status = data.get("status", "unknown")
            parse = combined
        return {"ok": bool(data.get("ok")),
                "failures": list(data.get("failures", [])),
                "cases": int(data.get("total_cases", 0) or 0),
                "log": "", "parse": parse}
    if isinstance(result, dict):
        return {"ok": bool(result.get("ok", result.get("success", False))),
                "failures": list(result.get("failures", [])),
                "cases": int(result.get("cases", 0) or 0),
                "log": str(result.get("log", "")),
                "parse": result.get("parse")}
    return {"ok": bool(getattr(result, "success", False)),
            "failures": list(getattr(result, "failures", []) or []),
            "cases": 0, "log": str(getattr(result, "output", "")),
            "parse": None}
