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
    def __init__(self, rules: dict[str, PermissionLevel] | None = None,
                 mode: OperationMode | str = OperationMode.ASSISTED) -> None:
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
