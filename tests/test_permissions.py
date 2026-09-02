from forge.security.permissions import (
    PermissionLevel,
    PermissionManager,
)


def test_safe_operation():
    permissions = PermissionManager()

    assert permissions.check("read_file") == PermissionLevel.SAFE


def test_push_requires_approval():
    permissions = PermissionManager()

    assert (
        permissions.check("git_push")
        == PermissionLevel.APPROVAL_REQUIRED
    )


def test_unknown_operation_is_not_automatically_allowed():
    permissions = PermissionManager()

    assert (
        permissions.check("unknown_operation")
        == PermissionLevel.APPROVAL_REQUIRED
    )
