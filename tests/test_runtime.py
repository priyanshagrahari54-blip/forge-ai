from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import PermissionManager


def test_read_file_is_allowed(tmp_path):
    file = tmp_path / "hello.txt"
    file.write_text("hello", encoding="utf-8")

    runtime = create_default_runtime(
        PermissionManager(),
        str(tmp_path),
    )

    result = runtime.execute(
        "read_file",
        path="hello.txt",
    )

    assert result.success
    assert result.output == "hello"


def test_write_requires_approval(tmp_path):
    runtime = create_default_runtime(
        PermissionManager(),
        str(tmp_path),
    )

    result = runtime.execute(
        "write_file",
        path="hello.txt",
        content="hello",
    )

    assert not result.success
    assert "Approval required" in result.error


def test_write_with_approval(tmp_path):
    runtime = create_default_runtime(
        PermissionManager(),
        str(tmp_path),
    )

    result = runtime.execute(
        "write_file",
        path="hello.txt",
        content="hello",
        approved=True,
    )

    assert result.success
    assert file_exists(tmp_path / "hello.txt")


def file_exists(path):
    return path.exists()


def test_path_escape_is_blocked(tmp_path):
    runtime = create_default_runtime(
        PermissionManager(),
        str(tmp_path),
    )

    result = runtime.execute(
        "read_file",
        path="../secret.txt",
    )

    assert not result.success
