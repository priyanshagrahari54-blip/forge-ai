from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.models.router import ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolRuntime
from forge.security.permissions import PermissionManager
from forge.tools.change_applier import ChangeApplier, CodeChange


@dataclass
class FailureReport:
    """Structured record of one failing test execution (A32.4).

    Every bounded retry is traceable to the exact command, exit code, and
    captured output that caused it, plus the recorded reason for the retry.
    Output is capped so a verbose failure cannot exhaust memory.
    """

    attempt_number: int
    command: list[str]
    exit_code: int | None
    output: str
    diagnosis: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DebugAttempt:
    attempt_number: int
    failure_error: str
    diagnosis: str
    modifications: dict[str, str]
    test_passed: bool
    test_output: str
    model: str = ""
    model_latency: float = 0.0
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    #: Recorded reason for this retry (empty only for legacy constructions).
    reason: str = ""
    #: Structured failure behind this attempt (None for a passing retest).
    failure: FailureReport | None = None


@dataclass
class DebugLoopResult:
    success: bool
    attempts: list[DebugAttempt] = field(default_factory=list)
    final_state: str = ""
    error: str = ""
    #: One structured report per failing test execution in order.
    failures: list[FailureReport] = field(default_factory=list)
    #: Test scope that ran: "targeted" names or "full suite".
    scope: str = "full suite"


class DebuggerAgent(AgentExecutor):
    name = "debugger"

    def __init__(self, root: str = ".", runtime: ToolRuntime | None = None, router: ModelRouter | None = None,
                 fabric: "ModelFabric | None" = None, approval_store=None,
                 model_policy=None):
        self.root = str(Path(root).resolve())
        self.runtime = runtime or create_default_runtime(PermissionManager(), self.root)
        if fabric is not None:
            self.fabric = fabric
            self.router = None
        elif router is not None:
            self.fabric = None
            self.router = router
        else:
            from forge.models.fabric import ModelFabric
            self.fabric = ModelFabric.from_defaults()
            self.router = None
        # Repairs are model output like any other change: they validate
        # through the ChangeSet engine and authorize through the policy gate.
        self.applier = ChangeApplier(self.runtime, root=self.root,
                                     approval_store=approval_store)
        self.model_policy = model_policy
        #: Policy decisions accumulated across repairs (observability).
        self.repair_decisions: list = []
        self.last_model = ""
        self.last_latency = 0.0

    def describe(self) -> str:
        return "Diagnoses test failures and applies bounded model-generated fixes."

    def diagnose(self, failure_output: str, source_code: str = "") -> str:
        return failure_output[-2000:] if failure_output else "Unknown test failure"

    def execute(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(True, output=self.diagnose(request.instructions or request.metadata.get("error", "")), agent=self.name, stage=request.stage)

    def _apply_repair(self, response_text: str, approved: bool, task_id: str = "",
                      approval_token_id: str = "") -> dict[str, str]:
        """Validate a repair proposal and apply it through the ChangeSet engine."""
        changes, extra = CoderAgent(root=self.root)._parse_changes(response_text)
        if not changes:
            raise ValueError("Debugger model proposed no changes")
        meta = extra.get("change_meta", {})
        result = self.applier.apply(
            [
                CodeChange(
                    path=path,
                    content=content,
                    risk=meta.get(path, {}).get("risk", "NONE"),
                    expected_old_hash=meta.get(path, {}).get("expected_old_hash"),
                    expected_old_content=meta.get(path, {}).get("expected_old_content"),
                )
                for path, content in changes.items()
            ],
            approved=approved,
            label="repair",
            capability="debugging",
            actor=self.name,
            task_id=task_id,
            approval_token_id=approval_token_id,
        )
        self.repair_decisions.extend(result.decisions)
        if not result.success:
            raise RuntimeError(result.errors[0] if result.errors else "repair rejected")
        return changes

    def repair(self, task: str, failure: str, context: str = "", approved: bool = True,
               previous_attempts: list[DebugAttempt] | None = None, task_id: str = "",
               approval_token_id: str = "") -> dict[str, str]:
        if self.fabric is not None:
            return self._repair_via_fabric(task, failure, context, approved, previous_attempts,
                                           task_id, approval_token_id)
        model = self.router.select("debugging") or self.router.select("coding")
        if not model or not model.provider:
            raise RuntimeError("No debugging model available; configure Ollama or another provider")
        self.last_model = model.name
        prompt = self._repair_prompt(task, failure, context, previous_attempts)
        try:
            response = model.provider.generate(prompt, context=context, task=task)
            self.last_latency = response.latency
            changes = self._apply_repair(response.text, approved, task_id,
                                         approval_token_id)
        except Exception:
            self.router.record(model.name, False, self.last_latency, capability="debugging", task_complexity=1.0)
            raise
        self.router.record(model.name, True, response.latency, capability="debugging", task_complexity=1.0)
        return changes

    @staticmethod
    def _repair_prompt(task: str, failure: str, context: str = "",
                       previous_attempts: list[DebugAttempt] | None = None) -> str:
        """Build the repair prompt with actual diagnostics and prior attempts.

        The model receives the command, captured output, and a compact history
        of previous repair attempts so it can change strategy instead of
        repeating the same failed fix.
        """
        parts = [
            "Diagnose and fix this test failure. Return ONLY JSON "
            "{changes:{relative/path:file contents}, explanation:str}. "
            "Make the smallest safe fix; do not modify tests to hide failures.",
            f"TASK:{task}",
            f"FAILURE:{failure}",
        ]
        if context:
            parts.append(f"CONTEXT:{context}")
        if previous_attempts:
            history = []
            for attempt in previous_attempts:
                history.append(
                    f"attempt {attempt.attempt_number}: "
                    f"exit_code={attempt.exit_code} model={attempt.model or '-'} "
                    f"modified={sorted(attempt.modifications) or '-'}"
                )
            parts.append("PREVIOUS ATTEMPTS:\n" + "\n".join(history))
        return "\n".join(parts)

    def _repair_via_fabric(self, task: str, failure: str, context: str = "", approved: bool = True,
                           previous_attempts: list[DebugAttempt] | None = None,
                           task_id: str = "",
                           approval_token_id: str = "") -> dict[str, str]:
        """Apply a model-generated repair through the centralized fabric.

        The fabric records provider-level feedback and telemetry; the returned
        structured change is validated through the ChangeSet engine and
        written only when the policy gate authorizes it. Model output never
        bypasses validation or write permissions.
        """
        from forge.models.request import ModelRequest

        prompt = self._repair_prompt(task, failure, context, previous_attempts)
        response = self.fabric.generate(ModelRequest(
            prompt=prompt,
            capability="debugging",
            required_capabilities=("debugging",),
            context=context,
            task=task,
            prefer_local=True,
            prefer_free=True,
            metadata={"model_data_policy": self.model_policy}
            if self.model_policy is not None else {},
        ))
        self.last_model = response.model
        self.last_latency = response.latency_ms
        if not response.success:
            raise RuntimeError(response.error or "No debugging model available; configure Ollama or another provider")
        return self._apply_repair(response.text, approved, task_id,
                                  approval_token_id)


class TestDebugLoop:
    # Not a pytest test class; prevents collection warnings when imported.
    __test__ = False

    def __init__(self, root: str | Path = ".", max_retries: int = 3,
                 debugger: DebuggerAgent | None = None, command: list[str] | None = None):
        self.root = str(root)
        self.max_retries = max(0, min(max_retries, 10))
        self.debugger = debugger or DebuggerAgent(self.root)
        # ``-B`` (interpreter) and ``no:cacheprovider`` stop stale bytecode and
        # last-failed caches from producing false results when a repair writes
        # an equal-size file within the same mtime granularity window.
        self.command = command or [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

    @staticmethod
    def _sanitize_test_paths(test_paths: list[str] | tuple[str, ...] | None) -> list[str]:
        """Keep only repository-relative test paths; drop anything unsafe."""
        safe: list[str] = []
        for candidate in test_paths or []:
            if not isinstance(candidate, str) or not candidate:
                continue
            parsed = PurePosixPath(candidate)
            if parsed.is_absolute() or ".." in parsed.parts or "\\" in candidate:
                continue
            if ".git" in parsed.parts or ".forge" in parsed.parts:
                continue
            safe.append(candidate)
        return safe

    def run(self, task: str, context: str = "", approved: bool = True,
            test_paths: list[str] | tuple[str, ...] | None = None,
            task_id: str = "", approval_token_id: str = "") -> DebugLoopResult:
        """Run tests, repairing bounded failures with structured reports.

        When ``test_paths`` names the tests relevant to the change, only those
        run here (the acceptance gate still runs the full suite, so skipped
        regressions cannot slip through). Otherwise the full suite runs, as
        before. Every failing execution produces a :class:`FailureReport` and
        every retry carries a recorded reason; a failure is never reported as
        a pass.
        """
        scoped = self._sanitize_test_paths(test_paths)
        command = list(self.command) + scoped
        scope = "targeted" if scoped else "full suite"
        attempts: list[DebugAttempt] = []
        failures: list[FailureReport] = []
        # The range has a fixed upper bound: initial test + max_retries repairs.
        # Test execution uses the constrained ``run_tests`` tool (SAFE: no write
        # approval needed) when the runtime provides it, falling back to the
        # approval-gated ``terminal`` tool for custom runtimes.
        test_tool = ("run_tests" if "run_tests" in self.debugger.runtime.tools
                     else "terminal")
        for number in range(1, self.max_retries + 2):
            result = self.debugger.runtime.execute(test_tool, approved=approved, command=command,
                                                   actor="debugger", task_id=task_id)
            combined = ((result.output or "") + result.metadata.get("stdout", "") + result.metadata.get("stderr", ""))
            output = combined or result.error or ""
            exit_code = result.metadata.get("returncode")
            no_tests = exit_code == 5 and (
                "no tests ran" in combined.lower() or "collected 0 items" in combined.lower()
            )
            if result.success or no_tests:
                # A successful final retest is itself recorded as a passing
                # attempt so run telemetry shows the repair -> pass transition,
                # not just the failures that preceded it.
                if result.success and attempts:
                    attempts.append(DebugAttempt(
                        attempt_number=number,
                        failure_error="",
                        diagnosis="",
                        modifications={},
                        test_passed=True,
                        test_output=output,
                        model=self.debugger.last_model,
                        model_latency=self.debugger.last_latency,
                        command=list(command),
                        exit_code=exit_code,
                        reason=f"retest passed after {len(attempts)} repair(s)",
                        failure=None,
                    ))
                return DebugLoopResult(True, attempts, "tests passed or no test suite",
                                       "", failures, scope)
            diagnosis = self.debugger.diagnose(output)
            if number > self.max_retries:
                failures.append(FailureReport(
                    attempt_number=number, command=list(command),
                    exit_code=exit_code, output=output[-4000:],
                    diagnosis=diagnosis,
                    reason=f"retry bound reached ({self.max_retries} repairs); "
                           f"{scope} tests still failing",
                ))
                return DebugLoopResult(False, attempts, "tests failed", output, failures,
                                       scope)
            reason = (f"attempt {number}: {scope} tests failed with exit "
                      f"{exit_code}; scheduling bounded repair {number} of "
                      f"{self.max_retries}")
            report = FailureReport(
                attempt_number=number, command=list(command),
                exit_code=exit_code, output=output[-4000:],
                diagnosis=diagnosis, reason=reason,
            )
            failures.append(report)
            try:
                changes = self.debugger.repair(task, output, context, approved, previous_attempts=attempts,
                                               task_id=task_id, approval_token_id=approval_token_id)
            except Exception as exc:
                attempts.append(DebugAttempt(
                    attempt_number=number, failure_error=output, diagnosis=diagnosis,
                    modifications={}, test_passed=False, test_output=output,
                    model=self.debugger.last_model, model_latency=self.debugger.last_latency,
                    command=list(command), exit_code=exit_code,
                    reason=f"{reason}; repair failed: {exc}",
                    failure=report,
                ))
                return DebugLoopResult(False, attempts, "repair failed", str(exc), failures,
                                       scope)
            attempts.append(DebugAttempt(
                attempt_number=number,
                failure_error=output,
                diagnosis=diagnosis,
                modifications=changes,
                test_passed=False,
                test_output=output,
                model=self.debugger.last_model,
                model_latency=self.debugger.last_latency,
                command=list(command),
                exit_code=exit_code,
                reason=reason,
                failure=report,
            ))
        return DebugLoopResult(False, attempts, "tests failed", "retry bound reached", failures)
