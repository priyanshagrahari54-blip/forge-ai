from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.models.router import ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolRuntime
from forge.security.permissions import PermissionManager


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


@dataclass
class DebugLoopResult:
    success: bool
    attempts: list[DebugAttempt] = field(default_factory=list)
    final_state: str = ""
    error: str = ""


class DebuggerAgent(AgentExecutor):
    name = "debugger"

    def __init__(self, root: str = ".", runtime: ToolRuntime | None = None, router: ModelRouter | None = None,
                 fabric: "ModelFabric | None" = None):
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
        self.last_model = ""
        self.last_latency = 0.0

    def describe(self) -> str:
        return "Diagnoses test failures and applies bounded model-generated fixes."

    def diagnose(self, failure_output: str, source_code: str = "") -> str:
        return failure_output[-2000:] if failure_output else "Unknown test failure"

    def execute(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(True, output=self.diagnose(request.instructions or request.metadata.get("error", "")), agent=self.name, stage=request.stage)

    def repair(self, task: str, failure: str, context: str = "", approved: bool = True) -> dict[str, str]:
        if self.fabric is not None:
            return self._repair_via_fabric(task, failure, context, approved)
        model = self.router.select("debugging") or self.router.select("coding")
        if not model or not model.provider:
            raise RuntimeError("No debugging model available; configure Ollama or another provider")
        self.last_model = model.name
        prompt = (
            "Diagnose and fix this test failure. Return ONLY JSON "
            "{changes:{relative/path:file contents}, explanation:str}. "
            "Make the smallest safe fix; do not modify tests to hide failures.\n"
            f"TASK:{task}\nFAILURE:{failure}\nCONTEXT:{context}"
        )
        try:
            response = model.provider.generate(prompt, context=context, task=task)
            self.last_latency = response.latency
            # Reuse the coder's complete structural/path/content validation. It
            # performs no writes; writes still happen below through ToolRuntime.
            changes = CoderAgent(root=self.root, router=self.router)._changes(response.text)
            if not changes:
                raise ValueError("Debugger model proposed no changes")
            for path, content in changes.items():
                result = self.runtime.execute("write_file", approved=approved, path=path, content=content)
                if not result.success:
                    raise RuntimeError(result.error or "permissioned repair write failed")
        except Exception:
            self.router.record(model.name, False, self.last_latency, capability="debugging", task_complexity=1.0)
            raise
        self.router.record(model.name, True, response.latency, capability="debugging", task_complexity=1.0)
        return changes

    def _repair_via_fabric(self, task: str, failure: str, context: str = "", approved: bool = True) -> dict[str, str]:
        """Apply a model-generated repair through the centralized fabric.

        The fabric records provider-level feedback and telemetry; the returned
        structured change is validated and written through ToolRuntime exactly
        like the legacy path. Model output never bypasses validation or write
        permissions.
        """
        from forge.models.request import ModelRequest

        prompt = (
            "Diagnose and fix this test failure. Return ONLY JSON "
            "{changes:{relative/path:file contents}, explanation:str}. "
            "Make the smallest safe fix; do not modify tests to hide failures.\n"
            f"TASK:{task}\nFAILURE:{failure}\nCONTEXT:{context}"
        )
        response = self.fabric.generate(ModelRequest(
            prompt=prompt,
            capability="debugging",
            required_capabilities=("debugging",),
            context=context,
            task=task,
            prefer_local=True,
            prefer_free=True,
        ))
        self.last_model = response.model
        self.last_latency = response.latency_ms
        if not response.success:
            raise RuntimeError(response.error or "No debugging model available; configure Ollama or another provider")
        changes = CoderAgent(root=self.root, fabric=self.fabric)._changes(response.text)
        if not changes:
            raise ValueError("Debugger model proposed no changes")
        for path, content in changes.items():
            result = self.runtime.execute("write_file", approved=approved, path=path, content=content)
            if not result.success:
                raise RuntimeError(result.error or "permissioned repair write failed")
        return changes


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

    def run(self, task: str, context: str = "", approved: bool = True) -> DebugLoopResult:
        attempts: list[DebugAttempt] = []
        # The range has a fixed upper bound: initial test + max_retries repairs.
        for number in range(1, self.max_retries + 2):
            result = self.debugger.runtime.execute("terminal", approved=approved, command=self.command)
            combined = ((result.output or "") + result.metadata.get("stdout", "") + result.metadata.get("stderr", ""))
            output = combined or result.error or ""
            no_tests = result.metadata.get("returncode") == 5 and (
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
                    ))
                return DebugLoopResult(True, attempts, "tests passed or no test suite")
            if number > self.max_retries:
                return DebugLoopResult(False, attempts, "tests failed", output)
            diagnosis = self.debugger.diagnose(output)
            try:
                changes = self.debugger.repair(task, output, context, approved)
            except Exception as exc:
                attempts.append(DebugAttempt(
                    attempt_number=number, failure_error=output, diagnosis=diagnosis,
                    modifications={}, test_passed=False, test_output=output,
                    model=self.debugger.last_model, model_latency=self.debugger.last_latency,
                ))
                return DebugLoopResult(False, attempts, "repair failed", str(exc))
            attempts.append(DebugAttempt(
                attempt_number=number,
                failure_error=output,
                diagnosis=diagnosis,
                modifications=changes,
                test_passed=False,
                test_output=output,
                model=self.debugger.last_model,
                model_latency=self.debugger.last_latency,
            ))
        return DebugLoopResult(False, attempts, "tests failed", "retry bound reached")
