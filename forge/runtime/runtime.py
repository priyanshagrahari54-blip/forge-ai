from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from forge.security.policy import translate_a32
from forge.security.policy_gate import PolicyDecision


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

    #: The only denial an approval token may satisfy; mode and block
    #: denials use different reasons and stay absolute.
    APPROVAL_DENIAL = "Approval required before executing this operation."

    def execute(
        self,
        tool_name: str,
        *,
        approved: bool = False,
        actor: str = "",
        task_id: str = "",
        approval_token_id: str = "",
        risk: str = "NONE",
        fingerprint: str = "",
        request_id: str = "",
        **kwargs: Any,
    ) -> ToolResult:

        if tool_name not in self.tools:
            return ToolResult.fail(
                tool_name,
                f"Unknown tool: {tool_name}",
            )

        tool = self.tools[tool_name]

        # Prefer the mode-aware policy when available (PermissionManager with
        # OperationMode); fall back to the original level check for custom
        # permission managers that only implement check().
        may_execute = getattr(self.permission_manager, "may_execute", None)
        token_ok = False
        if callable(may_execute):
            allowed, reason = may_execute(tool.permission, approved=approved)
            if (not allowed and reason == self.APPROVAL_DENIAL
                    and approval_token_id):
                redeem = getattr(self.permission_manager, "redeem_token",
                                 None)
                if callable(redeem):
                    path = kwargs.get("path")
                    token_ok, token_reason = redeem(
                        tool.permission, approval_token_id,
                        path=path if isinstance(path, str) else "",
                        risk=risk, agent=actor, task_id=task_id,
                        fingerprint=fingerprint, request_id=request_id)
                    if token_ok:
                        allowed, reason = True, ""
            if not allowed:
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs)
                return ToolResult.fail(tool_name, reason)
        else:
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

        # Fine-grained policy consultation (A33): explicit engine rules can
        # only tighten the A32 verdict above, never loosen it.
        consult = getattr(self.permission_manager, "evaluate_request", None)
        if callable(consult):
            decision, evaluation = consult(
                tool.permission, tool=tool_name, risk=risk,
                approved=approved or token_ok, agent=actor, task_id=task_id,
                approval_token_id=approval_token_id if not token_ok else "",
                fingerprint=fingerprint, request_id=request_id, call=kwargs)
            if decision == PolicyDecision.DENY:
                reason = evaluation.reason if evaluation is not None else \
                    "Denied by permission policy."
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs, decision=PolicyDecision.DENY)
                return ToolResult.fail(tool_name, reason)
            if decision == PolicyDecision.REQUIRE_APPROVAL:
                reason = (evaluation.reason if evaluation is not None else
                          "Approval required before executing this operation.")
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs,
                                 decision=PolicyDecision.REQUIRE_APPROVAL)
                return ToolResult.fail(tool_name, reason)

        self._audit_tool(tool, allowed=True, reason="", actor=actor,
                         task_id=task_id,
                         approval_token_id=approval_token_id, call=kwargs)

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

    def _audit_tool(self, tool: ToolDefinition, *, allowed: bool,
                    reason: str, actor: str, task_id: str,
                    approval_token_id: str, call: dict[str, Any],
                    decision: PolicyDecision | None = None) -> None:
        """Record the enforcement outcome when an audit log is attached."""
        audit = getattr(self.permission_manager, "audit", None)
        if audit is None:
            return
        translated = translate_a32(tool.permission)
        resource = translated[0].value if translated is not None else "tool"
        if decision is None:
            if allowed:
                decision = PolicyDecision.ALLOW
            elif "Approval required" in reason:
                decision = PolicyDecision.REQUIRE_APPROVAL
            else:
                decision = PolicyDecision.DENY
        path = call.get("path") if isinstance(call.get("path"), str) else ""
        audit.record_decision(
            agent=actor or getattr(self.permission_manager, "agent", "")
            or "unknown",
            resource=resource, operation=tool.permission, scope=path or "",
            decision=decision, reason=reason, task_id=task_id,
            approval_required=decision == PolicyDecision.REQUIRE_APPROVAL,
            approval_id=approval_token_id)
