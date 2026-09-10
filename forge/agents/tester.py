"""Tester agent: runs the repository test suite and reports honestly.

Unlike the debug loop (which repairs failures), the tester only executes
and reports: exit code, pass/fail verdict, targeted paths, and bounded
output. It writes nothing and repairs nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse

#: Mirrors the repository's canonical test invocation.
BASE_COMMAND: tuple[str, ...] = (
    "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
)

MAX_OUTPUT_CHARS = 6000
DEFAULT_TIMEOUT = 300.0


class TesterAgent(AgentExecutor):
    """Execute pytest (whole suite or targeted paths) and report results."""

    # Not a pytest test class; prevents collection warnings when imported.
    __test__ = False

    name = "tester"

    def __init__(self, root: str | Path = ".",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.root = Path(root).resolve()
        self.timeout = max(1.0, float(timeout))

    def describe(self) -> str:
        return "Responsible for running and evaluating tests."

    def _command(self, test_paths: tuple[str, ...]) -> list[str]:
        command = [sys.executable, *BASE_COMMAND]
        for candidate in test_paths:
            # Targeted paths must stay inside the repository; anything
            # else is dropped rather than executed.
            if not candidate or ".." in Path(candidate).parts:
                continue
            if Path(candidate).is_absolute():
                continue
            if (self.root / candidate).exists():
                command.append(candidate)
        return command

    def execute(self, request: AgentRequest) -> AgentResponse:
        metadata = request.metadata or {}
        raw_paths = metadata.get("tests_to_run") or metadata.get("tests") or ()
        if isinstance(raw_paths, str):
            raw_paths = (raw_paths,)
        test_paths = tuple(str(path) for path in raw_paths
                           if isinstance(path, str))
        command = self._command(test_paths)
        try:
            process = subprocess.run(
                command, cwd=self.root, text=True, capture_output=True,
                timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return AgentResponse(
                False, error=f"Test command exceeded {self.timeout:.0f}s",
                agent=self.name, stage=request.stage,
                metadata={"files": [], "timeout": True,
                          "command": [Path(command[0]).name, *command[1:]]})
        except OSError as exc:
            return AgentResponse(
                False, error=f"Test command failed to start: {exc}",
                agent=self.name, stage=request.stage,
                metadata={"files": []})
        output = (process.stdout or "") + (process.stderr or "")
        no_tests = (process.returncode == 5
                    and "no tests ran" in output.lower())
        passed = process.returncode == 0 or no_tests
        verdict = ("passed" if process.returncode == 0
                   else ("no tests collected" if no_tests else "failed"))
        summary = (f"tests {verdict} (exit={process.returncode}"
                   + (f", paths={len(test_paths)}" if test_paths else "")
                   + ")\n" + output[-MAX_OUTPUT_CHARS:])
        return AgentResponse(
            passed, output=summary, agent=self.name, stage=request.stage,
            error="" if passed else f"tests {verdict}",
            metadata={"files": [], "exit_code": process.returncode,
                      "verdict": verdict, "no_tests": no_tests,
                      "command": [Path(command[0]).name, *command[1:]]})
