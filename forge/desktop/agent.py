"""The policy-gated Desktop Agent execution pipeline (A35).

Every desktop action — including observations — flows through one
pipeline, in this exact order:

``DesktopRequest -> agent identity -> structural validation ->
task scope -> A33 PolicyGate -> risk classification + hard invariants ->
profile verdict -> approval if required -> provider execution ->
observation -> audit``

There is no path from request to execution that skips a layer, and no
second permission system: the A33 :class:`PermissionPolicy` remains the
single security authority for desktop actions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.core.report import redact
from forge.desktop.actions import DesktopActionKind, DesktopRequest, validate_request
from forge.desktop.profiles import DesktopProfile
from forge.desktop.provider import DesktopProvider, DesktopProviderError
from forge.desktop.risk import RiskAssessment, classify
from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRequest, Resource
from forge.security.policy_gate import PolicyDecision

#: Provider failure kinds that the caller may retry safely.
TRANSIENT_ERRORS = frozenset({"disconnected", "unavailable", "timeout"})

#: Desktop resource scope for each action kind (task-scope matching).
def resource_scope(request: DesktopRequest) -> str:
    kind = request.kind.value
    if kind in ("screenshot", "read_screen"):
        return "screen"
    if kind == "window_list":
        return "windows"
    if kind == "window":
        return f"window:{request.target}" if request.target else "windows"
    if kind in ("mouse_move", "mouse_click", "keyboard"):
        return "input"
    if kind == "launch":
        return f"app:{request.target}"
    if kind == "file_select":
        return "files"
    if kind == "file_access":
        return f"file:{request.target}"
    if kind == "clipboard":
        return "clipboard"
    if kind == "app_action":
        return f"app:{request.target}"
    if kind == "process_list":
        return "processes"
    if kind == "process":
        return f"process:{request.target}" if request.target else "processes"
    if kind == "system_info":
        return "system"
    return f"{kind}:{request.target}" if request.target else kind


@dataclass(frozen=True)
class DesktopAuthorization:
    """Full evaluation context for one request (before execution)."""

    allowed: bool
    decision: PolicyDecision
    reason: str = ""
    risk: str = "NONE"
    risk_reasons: tuple[str, ...] = ()
    hard_violations: tuple[str, ...] = ()
    profile_verdict: str = "DENY"
    approval_required: bool = False
    approval_request_id: str = ""
    permission_request: PermissionRequest | None = None
    scope_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.value,
            "reason": self.reason,
            "risk": self.risk,
            "risk_reasons": list(self.risk_reasons),
            "hard_violations": list(self.hard_violations),
            "profile_verdict": self.profile_verdict,
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
            "scope_ok": self.scope_ok,
        }


@dataclass
class DesktopActionResult:
    allowed: bool
    action: str
    target: str
    decision: str
    risk: str = "NONE"
    reasons: tuple[str, ...] = ()
    approval_required: bool = False
    approval_request_id: str = ""
    executed: bool = False
    observation: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    audit_events: int = 0
    duration_ms: float = 0.0

    @property
    def recoverable(self) -> bool:
        return bool(self.error and self.error.get("kind") in TRANSIENT_ERRORS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "target": self.target,
            "decision": self.decision,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
            "executed": self.executed,
            "observation": redact(dict(self.observation)),
            "error": self.error,
            "audit_events": self.audit_events,
            "duration_ms": round(self.duration_ms, 3),
        }


class GrantScopeChecker:
    """Task-scope authority for desktop actions.

    Desktop actuation may only happen inside an explicitly granted task
    scope. Interactive requests (empty ``task_id``) pass the scope layer
    but still face policy, risk, profile, and approval gates.
    """

    def __init__(self, *, ttl: float = 3600.0,
                 clock: Callable[[], float] | None = None) -> None:
        self.ttl = ttl
        self._clock = clock or time.time
        self._grants: dict[str, dict[str, Any]] = {}

    def grant(self, task_id: str, scopes: tuple[str, ...] | list[str], *,
              ttl: float | None = None) -> None:
        if not task_id:
            raise ValueError("task_id is required for a desktop grant")
        self._grants[task_id] = {
            "scopes": frozenset(scopes),
            "expires": self._clock() + (self.ttl if ttl is None else ttl),
        }

    def revoke(self, task_id: str) -> bool:
        return self._grants.pop(task_id, None) is not None

    def active(self, task_id: str) -> tuple[str, ...] | None:
        grant = self._grants.get(task_id)
        if grant is None or grant["expires"] <= self._clock():
            return None
        return tuple(sorted(grant["scopes"]))

    def check(self, task_id: str, request: DesktopRequest) -> tuple[bool, str]:
        if not task_id:
            return True, "interactive request (no task scope)"
        grant = self._grants.get(task_id)
        if grant is None:
            return False, f"task {task_id!r} has no desktop grant"
        if grant["expires"] <= self._clock():
            return False, f"task {task_id!r} desktop grant expired"
        scope = resource_scope(request)
        for granted in grant["scopes"]:
            if _covers(granted, scope):
                return True, ""
        return False, (f"scope {scope!r} outside task grant "
                       f"{sorted(grant['scopes'])!r}")


def _covers(granted: str, scope: str) -> bool:
    if granted == "*" or granted == scope:
        return True
    if granted.startswith("file:") and scope.startswith("file:"):
        granted_path = granted[5:].rstrip("/") + "/"
        return scope[5:].startswith(granted_path)
    if granted == "app:*" and scope.startswith("app:"):
        return True
    if granted == "window:*" and scope.startswith("window:"):
        return True
    if granted == "process:*" and scope.startswith("process:"):
        return True
    return False


class DesktopAgent:
    """Controlled desktop execution: policy-gated, risk-checked, audited."""

    def __init__(self, provider: DesktopProvider, *,
                 policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None,
                 profile: DesktopProfile | None = None,
                 scope_checker: Any | None = None) -> None:
        if provider is None:
            raise ValueError("DesktopAgent requires a provider")
        self.provider = provider
        # No policy means no authorization: fail closed.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit
        self.profile = profile or DesktopProfile()
        self.scope_checker = scope_checker or GrantScopeChecker()

    # -- authorization ---------------------------------------------------------

    def authorize(self, request: DesktopRequest, *,
                  profile: DesktopProfile | None = None
                  ) -> DesktopAuthorization:
        """Evaluate the full pipeline without executing anything."""
        effective_profile = profile or self.profile
        if not request.agent:
            return DesktopAuthorization(
                False, PolicyDecision.DENY, "missing agent identity")
        ok, err = validate_request(request)
        if not ok:
            return DesktopAuthorization(False, PolicyDecision.DENY,
                                        f"invalid request: {err}")
        # 1. Task scope.
        scope_ok, scope_reason = self.scope_checker.check(
            request.task_id, request)
        if not scope_ok:
            if self.audit is not None:
                self.audit.record_decision(
                    agent=request.agent, resource="desktop",
                    operation=request.policy_operation(),
                    decision=PolicyDecision.DENY,
                    scope=resource_scope(request), risk="NONE",
                    reason=scope_reason, task_id=request.task_id)
            return DesktopAuthorization(
                False, PolicyDecision.DENY, scope_reason, scope_ok=False)
        # 2. A33 policy gate — the single security authority.
        permission = PermissionRequest(
            agent=request.agent, resource=Resource.DESKTOP,
            operation=request.policy_operation(),
            scope=resource_scope(request),
            task_id=request.approval_task_id,
            reason=request.reason or f"desktop {request.kind.value}")
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.DENY:
            return DesktopAuthorization(False, PolicyDecision.DENY,
                                        evaluation.reason,
                                        permission_request=permission)
        # 3. Risk classification + hard invariants (always win).
        assessment: RiskAssessment = classify(request)
        if assessment.forbidden:
            reason = ("forbidden by hard security invariants: "
                      + "; ".join(assessment.hard_violations))
            if self.audit is not None:
                self.audit.record_decision(
                    agent=request.agent, resource="desktop",
                    operation=request.policy_operation(),
                    decision=PolicyDecision.DENY, scope=resource_scope(request),
                    risk=assessment.risk, reason=reason,
                    task_id=request.task_id)
            return DesktopAuthorization(
                False, PolicyDecision.DENY, reason, risk=assessment.risk,
                risk_reasons=assessment.reasons,
                hard_violations=assessment.hard_violations,
                permission_request=permission)
        # 4. Profile verdict (may only tighten policy).
        verdict = effective_profile.verdict_for(request.kind,
                                                assessment.risk)
        if verdict == "DENY":
            return DesktopAuthorization(
                False, PolicyDecision.DENY,
                f"profile {effective_profile.mode!r} denies "
                f"{request.kind.value}", risk=assessment.risk,
                risk_reasons=assessment.reasons,
                profile_verdict=verdict, permission_request=permission)
        approval_required = (
            evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
            or verdict == "APPROVAL")
        if approval_required:
            reason = (evaluation.reason if evaluation.decision
                      == PolicyDecision.REQUIRE_APPROVAL
                      else f"profile {effective_profile.mode!r} requires "
                      "approval")
            return DesktopAuthorization(
                False, PolicyDecision.REQUIRE_APPROVAL, reason,
                risk=assessment.risk, risk_reasons=assessment.reasons,
                profile_verdict=verdict, approval_required=True,
                permission_request=permission)
        return DesktopAuthorization(
            True, evaluation.decision, evaluation.reason,
            risk=assessment.risk, risk_reasons=assessment.reasons,
            profile_verdict=verdict, permission_request=permission)

    # -- execution --------------------------------------------------------------

    def act(self, request: DesktopRequest, *,
            approval_token_id: str = "",
            approve_callback: Callable[[DesktopRequest, DesktopAuthorization],
                                       str] | None = None,
            profile: DesktopProfile | None = None,
            ) -> DesktopActionResult:
        """Authorize and (when allowed) execute one desktop action."""
        started = time.monotonic()
        authorization = self.authorize(request, profile=profile)
        reasons = list(authorization.risk_reasons)
        if authorization.hard_violations:
            reasons.extend(authorization.hard_violations)
        if not authorization.allowed and authorization.reason \
                and authorization.reason not in reasons:
            reasons.append(authorization.reason)
        base = DesktopActionResult(
            allowed=authorization.allowed,
            action=request.kind.value, target=request.target,
            decision=authorization.decision.value,
            risk=authorization.risk,
            reasons=tuple(reasons),
            approval_required=authorization.approval_required,
            approval_request_id=authorization.approval_request_id)
        if not authorization.allowed and not authorization.approval_required:
            base.duration_ms = (time.monotonic() - started) * 1000
            return base
        # 5. Approval resolution.
        if authorization.approval_required:
            resolved, detail = self._resolve_approval(
                request, authorization, approval_token_id, approve_callback)
            base.approval_request_id = detail
            if not resolved:
                base.duration_ms = (time.monotonic() - started) * 1000
                return base
        # 6. Execution through the provider. Providers that cannot serve
        # must raise DesktopProviderError with a specific kind — the agent
        # never fabricates a generic failure over a real one.
        try:
            observation = self._dispatch(request)
        except DesktopProviderError as exc:
            return self._failure(base, started, exc.kind, exc.message)
        except Exception as exc:  # provider contract breach: fail closed
            return self._failure(base, started, "provider_error",
                                 f"provider raised {type(exc).__name__}")
        base.allowed = True
        base.executed = True
        base.decision = PolicyDecision.ALLOW.value
        base.observation = redact(observation)
        base.duration_ms = (time.monotonic() - started) * 1000
        if self.audit is not None:
            self.audit.record_decision(
                agent=request.agent, resource="desktop",
                operation=request.policy_operation(),
                decision=PolicyDecision.ALLOW,
                scope=resource_scope(request), risk=authorization.risk,
                reason=request.reason or f"desktop {request.kind.value}",
                task_id=request.task_id)
        return base

    def observe(self, request: DesktopRequest,
                **kwargs: Any) -> DesktopActionResult:
        """Run an observation action; actuation kinds are refused."""
        if not request.is_observation():
            return DesktopActionResult(
                False, request.kind.value, request.target,
                PolicyDecision.DENY.value,
                reasons=("observe() only accepts observation actions",))
        return self.act(request, **kwargs)

    # -- capability surface -------------------------------------------------------

    def available_actions(self, *,
                           profile: DesktopProfile | None = None
                           ) -> list[dict[str, Any]]:
        healthy = _safe_healthy(self.provider)
        effective_profile = profile or self.profile
        out = []
        for kind in DesktopActionKind:
            probe = DesktopRequest(kind, agent="capability-probe")
            assessment = classify(probe)
            verdict = effective_profile.verdict_for(kind, assessment.risk)
            out.append({
                "action": kind.value,
                "observation": probe.is_observation(),
                "risk": assessment.risk,
                "profile": verdict,
                "executable": healthy,
            })
        return out

    # -- internals -------------------------------------------------------------------

    def _resolve_approval(self, request: DesktopRequest,
                          authorization: DesktopAuthorization,
                          token_id: str,
                          approve_callback: Callable | None,
                          ) -> tuple[bool, str]:
        permission = authorization.permission_request
        if permission is None:
            return False, ""
        if approve_callback is not None:
            token = approve_callback(request, authorization)
            return bool(token), (token or "")
        if token_id:
            allowed, _reason = enforce_with_token(
                self.store, token_id, permission)
            if allowed:
                return True, token_id
            # The token did not redeem (used, expired, revoked, or out of
            # scope): fail closed and file a fresh approval request so the
            # result points at a request a human can actually decide.
            if self.store is None:
                return False, ""
            filed = self._file_approval(request, authorization)
            return False, filed.id
        if self.store is None:
            return False, ""
        filed = self._file_approval(request, authorization)
        return False, filed.id

    def _file_approval(self, request: DesktopRequest,
                       authorization: DesktopAuthorization) -> Any:
        filed = self.store.submit(ApprovalRequest(
            agent=request.agent, resource=Resource.DESKTOP,
            operation=request.policy_operation(),
            scopes=(resource_scope(request),),
            task_id=request.approval_task_id,
            reason=request.reason or f"Desktop {request.kind.value} on "
            f"{request.target or resource_scope(request)}",
            consequences=f"Risk {authorization.risk}: the agent will "
            "perform this desktop action."))
        return filed

    def _dispatch(self, request: DesktopRequest) -> dict[str, Any]:
        kind = request.kind.value
        target = request.target
        params = request.params
        if kind == "screenshot":
            return self.provider.screenshot()
        if kind == "read_screen":
            return self.provider.read_screen()
        if kind == "window_list":
            return self.provider.list_windows()
        if kind == "window":
            return self.provider.window(target, params)
        if kind == "mouse_move":
            return self.provider.mouse_move(target, params)
        if kind == "mouse_click":
            return self.provider.mouse_click(target, params)
        if kind == "keyboard":
            return self.provider.keyboard(target, params)
        if kind == "launch":
            return self.provider.launch(target, params)
        if kind == "file_select":
            return self.provider.file_select(target, params)
        if kind == "file_access":
            return self.provider.file_access(target, params)
        if kind == "clipboard":
            return self.provider.clipboard(target, params)
        if kind == "app_action":
            return self.provider.app_action(target, params)
        if kind == "process_list":
            return self.provider.list_processes()
        if kind == "process":
            return self.provider.process(target, params)
        if kind == "system_info":
            return self.provider.system_info()
        raise DesktopProviderError(
            "unsupported", f"no provider dispatch for {kind!r}")

    def _failure(self, base: DesktopActionResult, started: float,
                 kind: str, message: str) -> DesktopActionResult:
        base.allowed = False
        base.error = {"kind": kind, "message": message}
        base.duration_ms = (time.monotonic() - started) * 1000
        return base


def _safe_healthy(provider: DesktopProvider) -> bool:
    try:
        return bool(provider.healthy())
    except Exception:
        return False
