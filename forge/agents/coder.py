from __future__ import annotations

from typing import Any
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolResult, ToolRuntime
from forge.security.permissions import PermissionManager


class CoderAgent(AgentExecutor):
    name = "coder"

    def __init__(self, runtime: ToolRuntime | None = None, root: str = ".") -> None:
        self.root = root
        if runtime is None:
            pm = PermissionManager()
            self.runtime = create_default_runtime(pm, root=root)
        else:
            self.runtime = runtime

    def describe(self) -> str:
        return "Responsible for implementing software changes."

    def build_context(
        self,
        intelligence: RepositoryIntelligence,
        task: str,
        target_files: tuple[str, ...] = (),
        target_symbols: tuple[str, ...] = (),
        max_tokens: int = 4000,
    ) -> AgentContext:
        return AgentContextBuilder(
            intelligence,
            max_tokens=max_tokens,
        ).build(
            task=task,
            target_files=target_files,
            target_symbols=target_symbols,
        )

    def write_file(self, path: str, content: str, approved: bool = True) -> ToolResult:
        return self.runtime.execute(
            "write_file",
            approved=approved,
            path=path,
            content=content,
        )

    def read_file(self, path: str, approved: bool = True) -> ToolResult:
        return self.runtime.execute("read_file", approved=approved, path=path)

    def run_tests(
        self,
        command: list[str] | None = None,
        approved: bool = True,
    ) -> ToolResult:
        cmd = command or ["pytest", "-q"]
        return self.runtime.execute("terminal", approved=approved, command=cmd)

    def execute(self, request: AgentRequest) -> AgentResponse:
        changes = request.metadata.get("changes") if request.metadata else None
        approved_val = (
            request.metadata.get("approved", "true") if request.metadata else "true"
        )
        approved = str(approved_val).lower() == "true"

        applied = []
        if isinstance(changes, dict):
            for path, content in changes.items():
                res = self.write_file(path, content, approved=approved)
                if not res.success:
                    return AgentResponse(
                        success=False,
                        error=f"Failed to write file {path}: {res.error}",
                        agent=self.name,
                        stage=request.stage,
                    )
                applied.append(path)

        return AgentResponse(
            success=True,
            output=(
                f"Coder completed task for '{request.task.description}'. "
                f"Files modified: {applied if applied else 'none'}"
            ),
            agent=self.name,
            stage=request.stage,
        )
