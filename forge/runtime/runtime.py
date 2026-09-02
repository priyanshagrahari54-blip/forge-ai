from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class ToolResult:
    tool: str
    success: bool
    output: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        tool: str,
        output: str = "",
        duration_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            tool=tool,
            success=True,
            output=output,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

    @classmethod
    def fail(
        cls,
        tool: str,
        error: str,
        duration_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            tool=tool,
            success=False,
            error=error,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )


@dataclass
class ToolDefinition:
    name: str
    description: str
    handler: Callable[..., ToolResult]
    permission: str = "approval_required"


class ToolRuntime:
    def __init__(self, permission_manager) -> None:
        self.permission_manager = permission_manager
        self.tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Tool already registered: {tool.name}")

        self.tools[tool.name] = tool

    def list_tools(self) -> list[ToolDefinition]:
        return list(self.tools.values())

    def execute(
        self,
        tool_name: str,
        *,
        approved: bool = False,
        **kwargs: Any,
    ) -> ToolResult:

        if tool_name not in self.tools:
            return ToolResult.fail(
                tool_name,
                f"Unknown tool: {tool_name}",
            )

        tool = self.tools[tool_name]

        permission = self.permission_manager.check(tool.permission)

        if permission.value == "blocked":
            return ToolResult.fail(
                tool_name,
                "Operation blocked by security policy.",
            )

        if permission.value == "approval_required" and not approved:
            return ToolResult.fail(
                tool_name,
                "Approval required before executing this operation.",
            )

        started = datetime.now(timezone.utc)

        try:
            result = tool.handler(**kwargs)

            elapsed = (
                datetime.now(timezone.utc) - started
            ).total_seconds() * 1000

            result.duration_ms = elapsed

            return result

        except Exception as exc:
            elapsed = (
                datetime.now(timezone.utc) - started
            ).total_seconds() * 1000

            return ToolResult.fail(
                tool_name,
                str(exc),
                duration_ms=elapsed,
            )
