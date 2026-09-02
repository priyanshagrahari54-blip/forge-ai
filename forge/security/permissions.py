from enum import Enum


class PermissionLevel(str, Enum):
    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class PermissionManager:
    def __init__(self) -> None:
        self.rules = {
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

    def check(self, operation: str) -> PermissionLevel:
        return self.rules.get(
            operation,
            PermissionLevel.APPROVAL_REQUIRED,
        )
