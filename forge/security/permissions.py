from enum import Enum


class PermissionLevel(str, Enum):
    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class OperationMode(str, Enum):
    """Session-wide permission posture (A32.17).

    ``SAFE``        read/search/analyze only
    ``ASSISTED``    modifications require explicit approval (default)
    ``AUTONOMOUS``  approved project-scope writes allowed; destructive or
                    sensitive operations still require approval
    ``LOCKED``      no modifications

    Modes never allow an agent to escalate its own permissions: the per-tool
    permission level (``BLOCKED``/``APPROVAL_REQUIRED``/``SAFE``) is always
    consulted first, and a mode can only make a session *more* restrictive.
    """

    SAFE = "safe"
    ASSISTED = "assisted"
    AUTONOMOUS = "autonomous"
    LOCKED = "locked"


#: Operations considered read-only for the SAFE mode.
READ_OPERATIONS = frozenset({
    "read_file",
    "search_files",
    "git_status",
    "git_diff",
})

#: Non-destructive project-scope writes AUTONOMOUS mode may auto-approve.
AUTONOMOUS_AUTO_OPERATIONS = frozenset({
    "write_file",
})

#: Operations that always require explicit approval even in AUTONOMOUS mode.
SENSITIVE_OPERATIONS = frozenset({
    "delete_file",
    "delete_repository",
    "run_command",
    "git_commit",
    "git_push",
    "expose_secrets",
})


class PermissionManager:
    """Mode + per-operation levels (A32), optionally tightened by a policy.

    ``policy`` attaches a fine-grained :class:`PermissionPolicy
    <forge.security.policy.PermissionPolicy>` consulted through
    :meth:`evaluate_request`. The engine can only *tighten* an A32 verdict —
    explicit DENY / REQUIRE_APPROVAL rules add restrictions; engine silence
    (no matching rule) or ALLOW never loosens one. With no policy attached,
    behavior is byte-identical to A32.
    """

    def __init__(self, rules: dict[str, PermissionLevel] | None = None,
                 mode: OperationMode | str = OperationMode.ASSISTED,
                 *, policy=None, store=None, agent: str = "",
                 audit=None) -> None:
        self.policy = policy
        self.store = store
        self.agent = agent or ""
        self.audit = audit
        self.rules = dict(rules) if rules is not None else {
            "read_file": PermissionLevel.SAFE,
            "search_files": PermissionLevel.SAFE,
            "run_tests": PermissionLevel.SAFE,
            "git_status": PermissionLevel.SAFE,
            "git_diff": PermissionLevel.SAFE,

            "write_file": PermissionLevel.APPROVAL_REQUIRED,
            "delete_file": PermissionLevel.APPROVAL_REQUIRED,
            "run_command": PermissionLevel.APPROVAL_REQUIRED,
            "git_commit": PermissionLevel.APPROVAL_REQUIRED,
            "git_push": PermissionLevel.APPROVAL_REQUIRED,

            "delete_repository": PermissionLevel.BLOCKED,
            "expose_secrets": PermissionLevel.BLOCKED,
        }
        self.mode = OperationMode(mode)

    def check(self, operation: str) -> PermissionLevel:
        return self.rules.get(
            operation,
            PermissionLevel.APPROVAL_REQUIRED,
        )

    def may_execute(self, operation: str, *, approved: bool = False) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for an operation under the current mode.

        Mirrors ``ToolRuntime``'s original policy exactly under the default
        ``ASSISTED`` mode, so existing callers are unchanged.
        """
        level = self.check(operation)

        if self.mode == OperationMode.LOCKED and operation not in READ_OPERATIONS:
            return False, "operation blocked in LOCKED mode"
        if self.mode == OperationMode.SAFE and operation not in READ_OPERATIONS:
            return False, "operation blocked in SAFE mode"

        if level == PermissionLevel.BLOCKED:
            return False, "Operation blocked by security policy."

        if level == PermissionLevel.APPROVAL_REQUIRED and not approved:
            if self.mode == OperationMode.AUTONOMOUS and operation in AUTONOMOUS_AUTO_OPERATIONS:
                return True, ""
            return False, "Approval required before executing this operation."

        return True, ""

    def evaluate_request(self, operation: str, *, path: str = "",
                         tool: str = "", risk: str = "NONE",
                         capability: str = "", approved: bool = False,
                         agent: str = "", task_id: str = "",
                         request_id: str = "", approval_token_id: str = "",
                         fingerprint: str = "", preview: bool = False,
                         call: dict | None = None):
        """Consult the attached fine-grained policy (tighten-only).

        Returns ``(decision, evaluation)`` where ``decision`` is a
        :class:`PolicyDecision` or ``None`` when the engine has no opinion
        (no policy attached, untranslatable operation, or no matching rule).
        Callers combine a non-``None`` decision with the A32 verdict using
        :func:`most_restrictive`, so the engine can only tighten.
        """
        # Lazy import: the policy engine depends on the policy gate, which
        # depends on this module.
        from forge.security.policy import (
            PermissionRequest,
            scope_for_a32,
            translate_a32,
        )

        if self.policy is None:
            return None, None
        translated = translate_a32(operation)
        if translated is None:
            return None, None
        resource, engine_operation = translated
        call = dict(call or {})
        scope = path or scope_for_a32(operation, call)
        details: list[tuple[str, object]] = []
        command = call.get("command")
        if isinstance(command, list) and len(command) > 1:
            details.append(("args", tuple(str(item) for item in command[1:])))
        for key in ("host", "port", "protocol", "provider", "model",
                    "capability", "url", "domain"):
            if key in call:
                details.append((key, call[key]))
        if capability and "capability" not in call:
            details.append(("capability", capability))
        if tool:
            details.append(("tool", tool))
        from uuid import uuid4

        request = PermissionRequest(
            agent=agent or self.agent or "unknown", resource=resource,
            operation=engine_operation, scope=scope or "", risk=risk,
            task_id=task_id, request_id=request_id or uuid4().hex,
            details=tuple(details))
        evaluation = self.policy.evaluate(request)
        if self.audit is not None:
            self.audit.record_evaluation(
                request, evaluation, approval_id=approval_token_id)
        if not evaluation.matched_rules:
            # Engine silence (including default-deny) never tightens A32.
            return None, evaluation
        from forge.security.policy_gate import PolicyDecision

        if evaluation.decision == PolicyDecision.DENY:
            return PolicyDecision.DENY, evaluation
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            if approved:
                return PolicyDecision.ALLOW, evaluation
            if approval_token_id:
                allowed, _reason = self.redeem_token(
                    operation, approval_token_id, path=scope, risk=risk,
                    agent=request.agent, task_id=task_id,
                    fingerprint=fingerprint, request_id=request.request_id,
                    preview=preview, details=request.details)
                if allowed:
                    return PolicyDecision.ALLOW, evaluation
            return PolicyDecision.REQUIRE_APPROVAL, evaluation
        return PolicyDecision.ALLOW, evaluation

    def redeem_token(self, operation: str, token_id: str, *, path: str = "",
                     risk: str = "NONE", agent: str = "", task_id: str = "",
                     fingerprint: str = "", request_id: str = "",
                     preview: bool = False,
                     details: tuple = ()) -> tuple[bool, str]:
        """Redeem (or preview) an approval token for one A32 operation.

        Used by both the gate and the runtime layers; layers sharing a
        ``request_id`` redeem once per chain (see
        :meth:`ApprovalStore.redeem`). ``preview=True`` validates without
        consuming, for dry runs.
        """
        from forge.security.policy import PermissionRequest, translate_a32

        if not token_id or self.store is None:
            return False, "no approval token or store"
        translated = translate_a32(operation)
        if translated is None:
            return False, "operation is outside token scope"
        resource, engine_operation = translated
        from uuid import uuid4

        request = PermissionRequest(
            agent=agent or self.agent or "unknown", resource=resource,
            operation=engine_operation, scope=path, risk=risk,
            task_id=task_id, request_id=request_id or uuid4().hex,
            details=tuple(details))
        if preview:
            return self.store.check(token_id, request, fingerprint=fingerprint)
        return self.store.redeem(token_id, request, fingerprint=fingerprint)
