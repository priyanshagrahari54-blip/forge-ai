from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from forge.security.secrets import SecretRedactor


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
    def __init__(
        self,
        permission_manager,
        audit_log=None,
        redactor=None,
    ) -> None:
        self.permission_manager = permission_manager
        self.audit_log = audit_log
        self.redactor = redactor or SecretRedactor()
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
        started = datetime.now(timezone.utc)

        if tool_name not in self.tools:
            result = ToolResult.fail(
                tool_name,
                f"Unknown tool: {tool_name}",
            )
            self._audit(
                tool_name,
                started,
                "error",
                level="error",
                message=result.error or "",
            )
            return result

        tool = self.tools[tool_name]

        permission = self.permission_manager.check(tool.permission)

        if permission.value == "blocked":
            result = ToolResult.fail(
                tool_name,
                "Operation blocked by security policy.",
            )
            self._audit(
                tool_name,
                started,
                "blocked",
                level="warning",
                message=result.error or "",
            )
            return result

        if permission.value == "approval_required" and not approved:
            result = ToolResult.fail(
                tool_name,
                "Approval required before executing this operation.",
            )
            self._audit(
                tool_name,
                started,
                "denied",
                level="warning",
                message=result.error or "",
            )
            return result

        try:
            result = tool.handler(**kwargs)

            elapsed = (
                datetime.now(timezone.utc) - started
            ).total_seconds() * 1000

            result.duration_ms = elapsed

        except Exception as exc:
            elapsed = (
                datetime.now(timezone.utc) - started
            ).total_seconds() * 1000

            result = ToolResult.fail(
                tool_name,
                str(exc),
                duration_ms=elapsed,
            )

        self._sanitize(result)
        self._audit(
            tool_name,
            started,
            "success" if result.success else "error",
            level="info" if result.success else "error",
            message=result.output if result.success else (result.error or ""),
        )

        return result

    def _sanitize(self, result: ToolResult) -> None:
        """Redact detected secrets from tool output and error text."""
        if result.output:
            safe_output, findings = self.redactor.redact(result.output)

            if findings:
                result.output = safe_output
                result.metadata["redacted"] = True

        if result.error:
            safe_error, _findings = self.redactor.redact(result.error)
            result.error = safe_error

    def _audit(
        self,
        tool_name: str,
        started: datetime,
        result: str,
        *,
        level: str,
        message: str,
    ) -> None:
        if self.audit_log is None:
            return

        elapsed = (
            datetime.now(timezone.utc) - started
        ).total_seconds() * 1000

        self.audit_log.record(
            operation=tool_name,
            level=level,
            actor="runtime",
            result=result,
            message=message[:200],
        )
