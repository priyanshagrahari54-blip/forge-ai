"""Bounded native debug loop (A81 layer 9).

The exact cycle the requirement names, with one hard rule per stage:

``failure → classify → collect context → repair plan → authorized repair →
verify → repeat (bounded)``

* **classify** — deterministic failure categorization through the reasoning
  hub (syntax/import/collection/assertion/timeout/fixture/unknown).
* **collect context** — file/line locations parsed out of the traceback are
  resolved against the repository and *read through the permissioned
  runtime*, bounded to a handful of files for low-RAM hosts.
* **repair plan** — always requested through the reasoning hub; the
  deterministic backend refuses with ``NEURAL_REQUIRED`` (a repair is
  generative content), and that refusal is recorded as the stop reason
  instead of a fake fix.
* **authorized repair** — validated by the ChangeSet engine's dry run, then
  applied through the policy gate via the coding engine. A denied or blocked
  repair fails the cycle honestly; nothing is force-written and nothing is
  retried past the bound.
* **verify** — the targeted test command is re-executed for real; the engine
  runs the full verification suite afterwards. Failures stay failures.

Retry bound: ``max_retries`` repairs after the initial failing run, clamped
to ``0..10`` exactly like the A32 loop, so behavior on tiny machines stays
predictable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.native.reasoning import ReasoningKind, ReasoningRequest, RefusalCode

#: Traceback frames like ``File "src/pkg/mod.py", line 12`` (unittest/py).
_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
#: Pytest short traceback frames: ``calc.py:5: in add`` / ``a/b.py:12: in f``.
_PYTEST_FRAME = re.compile(
    r"^\s*([\w.\-/\\\\ ]*?[\w.\-]+\.py):(\d+)(?::\s*in\b|:)", re.M)

#: Bounded per-cycle context collection (2 GB host, not a debugger IDE).
MAX_FAILURE_FILES = 3
MAX_FAILURE_FILE_CHARS = 4000

#: Deterministic failure classes (mirrors the reasoning backend's rules).
FAILURE_CLASSES = (
    "syntax", "import", "collection", "assertion", "timeout", "fixture",
    "unknown",
)


@dataclass
class DebugCycle:
    """One recorded debug cycle (never optimistic: fields default to the
    failure they describe)."""

    attempt_number: int
    classification: str = "unknown"
    evidence: List[str] = field(default_factory=list)
    failure_context_files: List[str] = field(default_factory=list)
    repair: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    apply: Dict[str, Any] = field(default_factory=dict)
    retest: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    stopped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "classification": self.classification,
            "evidence": list(self.evidence),
            "failure_context_files": list(self.failure_context_files),
            "repair": dict(self.repair),
            "validation": dict(self.validation),
            "apply": dict(self.apply),
            "retest": dict(self.retest),
            "reason": self.reason,
            "stopped": self.stopped,
        }


@dataclass
class NativeDebugResult:
    success: bool
    cycles: List[DebugCycle] = field(default_factory=list)
    initial_run: Optional[Dict[str, Any]] = None
    final_run: Optional[Dict[str, Any]] = None
    #: Why the loop stopped: tests_passed | neural_required | policy_blocked |
    #: backend_error | invalid_proposal | retry_bound_reached |
    #: apply_failed | not_executed.
    stopped_reason: str = ""
    failures: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def repairs_attempted(self) -> int:
        return sum(1 for cycle in self.cycles if cycle.apply)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "initial_run": dict(self.initial_run or {}),
            "final_run": dict(self.final_run or {}),
            "stopped_reason": self.stopped_reason,
            "repairs_attempted": self.repairs_attempted,
            "failures": [dict(f) for f in self.failures],
        }


class NativeDebugLoop:
    """Drives the bounded repair cycles over the coding engine's primitives."""

    def __init__(self, coding: Any, hub: Any, max_retries: int = 3,
                 root: str | Path = ".") -> None:
        self.coding = coding
        self.hub = hub
        self.max_retries = max(0, min(int(max_retries), 10))
        self.root = Path(root).resolve()

    # -- helpers ------------------------------------------------------------

    def _failure_context(self, output: str) -> Tuple[str, List[str]]:
        """Resolve traceback frames to repository files and read them
        through the permissioned runtime (bounded)."""
        candidates: List[Tuple[str, int]] = []
        seen = set()
        matches = list(_FRAME.finditer(output or ""))
        matches += list(_PYTEST_FRAME.finditer(output or ""))
        for match in matches:
            raw = match.group(1).strip().replace("\\", "/")
            line = int(match.group(2))
            try:
                relative = str(Path(raw).resolve().relative_to(self.root))
            except (ValueError, OSError):
                relative = raw
            relative = relative.replace("\\", "/")
            if relative in seen:
                continue
            seen.add(relative)
            candidates.append((relative, line))
        snippets: List[str] = []
        used_files: List[str] = []
        for relative, line in candidates:
            if len(used_files) >= MAX_FAILURE_FILES:
                break
            parsed = PurePosixPath(relative)
            if parsed.is_absolute() or ".." in parsed.parts:
                continue
            if not (self.root / relative).is_file():
                continue
            used_files.append(relative)
            inspection = self.coding.inspect_file(relative,
                                                   MAX_FAILURE_FILE_CHARS)
            if inspection.ok:
                snippets.append("FILE %s (around line %d):\n%s"
                                % (relative, line, inspection.content))
            else:
                snippets.append("FILE %s: unreadable (%s)"
                                % (relative, inspection.error))
        return "\n\n".join(snippets), used_files

    # -- main loop ------------------------------------------------------------

    def run(self, task: str, context_text: str = "",
            test_paths: Optional[Sequence[str]] = None,
            approved: bool = False, task_id: str = "") -> NativeDebugResult:
        result = NativeDebugResult(False)
        run = self.coding.run_tests(test_paths, task_id=task_id)
        result.initial_run = run.to_dict()
        result.final_run = run.to_dict()
        if not run.executed:
            result.stopped_reason = "not_executed"
            result.failures.append({"reason": "tests could not run",
                                    "error": run.error})
            return result
        if run.passed:
            result.success = True
            result.stopped_reason = "tests_passed"
            return result
        if not run.output.strip():
            # Nothing to diagnose (e.g. the tool itself refused): the run
            # stays a failure; fabricating a diagnosis would be worse.
            result.stopped_reason = "not_executed"
            result.failures.append({"reason": run.error or
                                    "test run produced no output"})
            return result

        for attempt in range(1, self.max_retries + 1):
            cycle = DebugCycle(attempt_number=attempt)
            result.failures.append({"attempt_number": attempt,
                                    "exit_code": run.exit_code,
                                    "output_tail": run.output[-2000:]})
            # 1/2. classify deterministically through the hub.
            diagnosis = self.hub.respond(ReasoningRequest(
                kind=ReasoningKind.DIAGNOSE, task=task,
                payload={"failure_output": run.output[-12000:]}))
            if diagnosis.ok:
                cycle.classification = str(diagnosis.data.get(
                    "category", "unknown"))
                cycle.evidence = [str(line) for line in diagnosis.data.get(
                    "evidence_lines", [])][:5]
            else:
                cycle.reason = "diagnosis failed: %s" % diagnosis.message
                cycle.stopped = True
                result.cycles.append(cycle)
                result.stopped_reason = "backend_error"
                break

            # 3. collect failure context (real files, bounded, via tools).
            failure_context, context_files = self._failure_context(run.output)
            cycle.failure_context_files = context_files

            # 4. repair plan through the reasoning hub (neural-gated).
            proposal = self.coding.propose_changes(
                task + "\n\nCurrent failure (category: %s):\n%s"
                % (cycle.classification, run.output[-6000:]),
                context_text,
                kind=ReasoningKind.REPAIR,
                payload={"failure_output": run.output[-8000:],
                         "failure_context": failure_context})
            cycle.repair = proposal.to_dict()
            if not proposal.ok:
                if proposal.refusal == RefusalCode.NEURAL_REQUIRED.value:
                    cycle.reason = ("repair generation requires a neural "
                                    "backend; refusing to fabricate")
                    cycle.stopped = True
                    result.stopped_reason = "neural_required"
                elif proposal.refusal == RefusalCode.BACKEND_ERROR.value:
                    cycle.reason = "model backend failed: %s" % \
                        proposal.message[:300]
                    cycle.stopped = True
                    result.stopped_reason = "backend_error"
                else:
                    cycle.reason = "invalid model output: %s" % \
                        proposal.message[:300]
                    cycle.stopped = True
                    result.stopped_reason = "invalid_proposal"
                result.cycles.append(cycle)
                break

            # 5a. validate the proposal structurally (zero writes).
            validation = self.coding.validate_changes(proposal.changes,
                                                      approved=approved,
                                                      capability="debugging")
            cycle.validation = validation.to_dict()
            if not validation.ok:
                cycle.reason = ("repair proposal rejected by the ChangeSet "
                                "engine: %s"
                                % (validation.issues[0].reason
                                   if validation.issues else "invalid"))
                cycle.stopped = True
                result.stopped_reason = "invalid_proposal"
                result.cycles.append(cycle)
                break

            # 5b. apply the authorized repair (policy gate decides).
            outcome = self.coding.apply_changes(
                proposal.changes, approved=approved,
                capability="debugging", task_id=task_id,
                label="native-ai-repair")
            cycle.apply = outcome.to_dict()
            if not outcome.ok:
                if outcome.blocked_by_approval:
                    cycle.reason = "repair blocked by policy gate " \
                                   "(approval required/denied)"
                    cycle.stopped = True
                    result.stopped_reason = "policy_blocked"
                else:
                    cycle.reason = "repair apply failed: %s" % \
                        (outcome.errors[0][:300] if outcome.errors
                         else "unknown")
                    cycle.stopped = True
                    result.stopped_reason = "apply_failed"
                result.cycles.append(cycle)
                break

            # 6. verify with a real re-run, then repeat within the bound.
            run = self.coding.run_tests(test_paths, task_id=task_id)
            result.final_run = run.to_dict()
            cycle.retest = run.to_dict()
            cycle.reason = ("retest passed" if run.passed else
                            "retest still failing (exit %s)"
                            % run.exit_code)
            result.cycles.append(cycle)
            if run.passed:
                result.success = True
                result.stopped_reason = "tests_passed"
                return result
        else:
            result.stopped_reason = "retry_bound_reached"
            if self.max_retries == 0:
                cycle = DebugCycle(0)
                cycle.reason = "retry budget is 0; no repair attempted"
                cycle.stopped = True
                result.cycles.append(cycle)
        if not result.success and not result.stopped_reason:
            result.stopped_reason = "retry_bound_reached"
        return result
