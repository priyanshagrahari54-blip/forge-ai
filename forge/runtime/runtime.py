from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from forge.security.policy import translate_a32
from forge.security.policy_gate import PolicyDecision
from forge.core.fencing import FenceRegistry, commit_guard as make_commit_guard


@dataclass
class ToolResult:
    tool: str
    success: bool
    output: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, tool: str, output: str = "", duration_ms: float = 0.0,
           metadata: dict[str, Any] | None = None) -> "ToolResult":
        return cls(tool=tool, success=True, output=output,
                   duration_ms=duration_ms, metadata=metadata or {})

    @classmethod
    def fail(cls, tool: str, error: str, duration_ms: float = 0.0,
             metadata: dict[str, Any] | None = None) -> "ToolResult":
        return cls(tool=tool, success=False, error=error,
                   duration_ms=duration_ms, metadata=metadata or {})


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

    APPROVAL_DENIAL = "Approval required before executing this operation."

    MUTATING_PERMISSIONS = frozenset({
        "write_file", "delete_file", "run_command",
        "git_commit", "git_push", "deploy", "release",
    })

    def execute(self, tool_name: str, *, approved: bool = False,
                actor: str = "", task_id: str = "", approval_token_id: str = "",
                risk: str = "NONE", fingerprint: str = "", request_id: str = "",
                commit_guard: Any = None, **kwargs: Any) -> ToolResult:
        if tool_name not in self.tools:
            return ToolResult.fail(tool_name, f"Unknown tool: {tool_name}")

        tool = self.tools[tool_name]
        is_mutating = tool.permission in self.MUTATING_PERMISSIONS

        # A real worker supplies the scheduler-owned fence. Direct local agent
        # runs (the trusted in-process API used by the supervisor and tests)
        # must mint their own short-lived attempt fence rather than bypassing
        # the fence requirement. Remote callers cannot set ``approved=True``;
        # token approval is still checked by the policy layer below.
        local_fences: FenceRegistry | None = None
        local_fence: Any = None
        effective_guard = commit_guard
        if is_mutating and task_id and effective_guard is None:
            if not approved and not approval_token_id:
                reason = "NO_FENCE_AUTHORITY: task-bound mutation requires an execution fence."
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs, decision=PolicyDecision.DENY)
                return ToolResult.fail(
                    tool_name, reason,
                    metadata={"fenced": True, "error_code": "NO_FENCE_AUTHORITY"})
            local_fences = FenceRegistry()
            local_fence = local_fences.begin(task_id, owner=actor)
            local_fence = local_fences.mark_running(task_id, local_fence)
            effective_guard = make_commit_guard(local_fence, local_fences)

        if effective_guard is not None and is_mutating:
            try:
                fenced_reason = effective_guard()
            except Exception as exc:
                fenced_reason = f"commit guard errored: {exc}"
            if fenced_reason:
                return ToolResult.fail(
                    tool_name,
                    f"Fenced by execution attempt: {fenced_reason}",
                    metadata={"fenced": True, "reason": fenced_reason[:500]},
                )

        may_execute = getattr(self.permission_manager, "may_execute", None)
        token_ok = False
        if callable(may_execute):
            allowed, reason = may_execute(tool.permission, approved=approved)
            if (not allowed and reason == self.APPROVAL_DENIAL
                    and approval_token_id):
                redeem = getattr(self.permission_manager, "redeem_token", None)
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
                return ToolResult.fail(tool_name, "Operation blocked by security policy.")
            if permission.value == "approval_required" and not approved:
                return ToolResult.fail(tool_name, self.APPROVAL_DENIAL)

        consult = getattr(self.permission_manager, "evaluate_request", None)
        if callable(consult):
            decision, evaluation = consult(
                tool.permission, tool=tool_name, risk=risk,
                approved=approved or token_ok, agent=actor, task_id=task_id,
                approval_token_id=approval_token_id if not token_ok else "",
                fingerprint=fingerprint, request_id=request_id, call=kwargs)
            if decision == PolicyDecision.DENY:
                reason = evaluation.reason if evaluation is not None else "Denied by permission policy."
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs, decision=PolicyDecision.DENY)
                return ToolResult.fail(tool_name, reason)
            if decision == PolicyDecision.REQUIRE_APPROVAL:
                reason = (evaluation.reason if evaluation is not None else
                          self.APPROVAL_DENIAL)
                self._audit_tool(tool, allowed=False, reason=reason,
                                 actor=actor, task_id=task_id,
                                 approval_token_id=approval_token_id,
                                 call=kwargs,
                                 decision=PolicyDecision.REQUIRE_APPROVAL)
                return ToolResult.fail(tool_name, reason)

        self._audit_tool(tool, allowed=True, reason="", actor=actor,
                         task_id=task_id, approval_token_id=approval_token_id,
                         call=kwargs)
        started = datetime.now(timezone.utc)
        try:
            result = tool.handler(**kwargs)
            result.duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            if local_fences is not None and local_fence is not None:
                local_fences.commit(task_id, local_fence,
                                    "succeeded" if result.success else "failed")
            return result
        except Exception as exc:
            if local_fences is not None and local_fence is not None:
                try:
                    local_fences.commit(task_id, local_fence, "failed")
                except Exception:
                    pass
            elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            return ToolResult.fail(tool_name, str(exc), duration_ms=elapsed)

    def _audit_tool(self, tool: ToolDefinition, *, allowed: bool,
                    reason: str, actor: str, task_id: str,
                    approval_token_id: str, call: dict[str, Any],
                    decision: PolicyDecision | None = None) -> None:
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
            agent=actor or getattr(self.permission_manager, "agent", "") or "unknown",
            resource=resource, operation=tool.permission, scope=path or "",
            decision=decision, reason=reason, task_id=task_id,
            approval_required=decision == PolicyDecision.REQUIRE_APPROVAL,
            approval_id=approval_token_id)
