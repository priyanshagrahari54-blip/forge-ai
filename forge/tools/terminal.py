import subprocess
from forge.runtime.runtime import ToolResult


class TerminalTool:
    def __init__(self, root: str = ".") -> None:
        self.root = root

    def run(
        self,
        command: list[str],
        timeout: int = 30,
    ) -> ToolResult:

        if not command:
            return ToolResult.fail(
                "terminal.run",
                "Command cannot be empty.",
            )

        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )

            output = result.stdout

            if result.stderr:
                output += result.stderr

            if result.returncode != 0:
                return ToolResult.fail(
                    "terminal.run",
                    f"Command exited with code {result.returncode}.",
                    metadata={
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                )

            return ToolResult.ok(
                "terminal.run",
                output=output,
                metadata={
                    "returncode": result.returncode,
                },
            )

        except subprocess.TimeoutExpired:
            return ToolResult.fail(
                "terminal.run",
                f"Command timed out after {timeout} seconds.",
            )
