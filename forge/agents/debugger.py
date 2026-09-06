from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.core.task_engine import Task, TaskStatus
from forge.runtime.runtime import ToolResult


@dataclass
class DebugAttempt:
    attempt_number: int
    failure_error: str
    diagnosis: str
    modifications: dict[str, str]
    test_passed: bool
    test_output: str


@dataclass
class DebugLoopResult:
    success: bool
    attempts: list[DebugAttempt]
    final_state: str
    error: str = ""


class DebuggerAgent(AgentExecutor):
    name = "debugger"

    def describe(self) -> str:
        return "Responsible for analyzing failures and proposing fixes."

    def diagnose(self, failure_output: str, source_code: str = "") -> str:
        if "SyntaxError" in failure_output or "invalid syntax" in failure_output:
            return "Syntax error in implementation file."
        if "AssertionError" in failure_output:
            return f"Assertion failed: {failure_output.strip().splitlines()[-1]}"
        if "ModuleNotFoundError" in failure_output or "ImportError" in failure_output:
            return "Missing dependency or import issue."
        return f"Test failure analysis: {failure_output[:200]}"

    def propose_fix(self, failure_output: str, file_path: str = "", current_content: str = "") -> str:
        # Diagnostic heuristic for mock/test fix suggestions
        if "AssertionError" in failure_output and "expected" in failure_output:
            return current_content
        return current_content

    def execute(self, request: AgentRequest) -> AgentResponse:
        error_info = request.instructions or request.metadata.get("error", "Unknown test failure")
        diagnosis = self.diagnose(error_info)
        return AgentResponse(
            success=True,
            output=f"Diagnosis: {diagnosis}",
            agent=self.name,
            stage=request.stage,
        )


class TestDebugLoop:
    """Bounded closed-loop execution: CODE -> TEST -> DIAGNOSE -> DEBUG -> MODIFY -> TEST AGAIN."""

    __test__ = False

    def __init__(
        self,
        debugger: DebuggerAgent | None = None,
        max_retries: int = 3,
    ) -> None:
        self.debugger = debugger or DebuggerAgent()
        self.max_retries = max_retries

    def run(
        self,
        task: Task,
        test_runner: Callable[[], ToolResult],
        fix_applier: Callable[[str], dict[str, str]] | None = None,
    ) -> DebugLoopResult:
        attempts: list[DebugAttempt] = []

        for attempt_num in range(1, self.max_retries + 1):
            task.attempts = attempt_num
            test_res = test_runner()

            if test_res.success:
                attempts.append(
                    DebugAttempt(
                        attempt_number=attempt_num,
                        failure_error="",
                        diagnosis="Tests passed successfully.",
                        modifications={},
                        test_passed=True,
                        test_output=test_res.output,
                    )
                )
                task.status = TaskStatus.TESTING
                return DebugLoopResult(
                    success=True,
                    attempts=attempts,
                    final_state="PASSED",
                )

            failure = test_res.error or test_res.output
            task.errors.append(failure)
            diagnosis = self.debugger.diagnose(failure)

            mods: dict[str, str] = {}
            if fix_applier is not None:
                mods = fix_applier(diagnosis)

            attempts.append(
                DebugAttempt(
                    attempt_number=attempt_num,
                    failure_error=failure,
                    diagnosis=diagnosis,
                    modifications=mods,
                    test_passed=False,
                    test_output=test_res.output,
                )
            )

        task.status = TaskStatus.FAILED
        return DebugLoopResult(
            success=False,
            attempts=attempts,
            final_state="FAILED",
            error=f"Exhausted maximum retry attempts ({self.max_retries}).",
        )
