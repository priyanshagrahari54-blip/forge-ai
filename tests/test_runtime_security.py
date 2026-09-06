from forge.runtime.defaults import create_default_runtime
from forge.security.audit import AuditLog


def make_runtime(tmp_path, audit_log):
    from forge.security.permissions import PermissionManager

    return create_default_runtime(
        PermissionManager(),
        root=str(tmp_path),
        audit_log=audit_log,
    )


def test_read_file_redacts_secret_contents(tmp_path):
    from forge.security.permissions import PermissionManager

    path = tmp_path / "credentials.txt"
    path.write_text(
        "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )

    runtime = make_runtime(tmp_path, AuditLog())

    result = runtime.execute(
        "read_file",
        path="credentials.txt",
    )

    assert result.success
    assert "AKIAIOSFODNN7EXAMPLE" not in result.output
    assert "<redacted>" in result.output
    assert result.metadata.get("redacted") is True


def test_audit_records_denied_approval(tmp_path):
    from forge.security.permissions import PermissionManager

    log = AuditLog()
    runtime = make_runtime(tmp_path, log)

    result = runtime.execute(
        "write_file",
        path="hello.txt",
        content="hello",
    )

    assert not result.success

    denied = [
        entry
        for entry in log.entries
        if entry.operation == "write_file"
    ]

    assert denied
    assert denied[0].result == "denied"


def test_audit_records_successful_read(tmp_path):
    from forge.security.permissions import PermissionManager

    path = tmp_path / "hello.txt"
    path.write_text("hello", encoding="utf-8")

    log = AuditLog()
    runtime = make_runtime(tmp_path, log)

    result = runtime.execute(
        "read_file",
        path="hello.txt",
    )

    assert result.success

    reads = [
        entry
        for entry in log.entries
        if entry.operation == "read_file"
    ]

    assert reads
    assert reads[0].result == "success"
    assert reads[0].message == "hello"


def test_scan_secrets_tool_reports_redacted_findings(tmp_path):
    from forge.security.permissions import PermissionManager

    log = AuditLog()
    path = tmp_path / "config.py"
    path.write_text(
        "token = 'ghp_" + "A" * 36 + "'\n",
        encoding="utf-8",
    )

    runtime = make_runtime(tmp_path, log)

    result = runtime.execute(
        "scan_secrets",
        path="config.py",
    )

    assert result.success

    findings = result.metadata["findings"]

    assert len(findings) == 1
    assert findings[0]["type"] == "github_token"
    assert "ghp_" not in findings[0]["snippet"]
    assert "<redacted>" in findings[0]["snippet"]


def test_runtime_without_audit_log_still_redacts(tmp_path):
    from forge.security.permissions import PermissionManager

    path = tmp_path / "config.txt"
    path.write_text(
        "password=hunter2secret",
        encoding="utf-8",
    )

    runtime = create_default_runtime(
        PermissionManager(),
        root=str(tmp_path),
    )

    assert runtime.audit_log is None

    result = runtime.execute(
        "read_file",
        path="config.txt",
    )

    assert result.success
    assert "hunter2secret" not in result.output
    assert "<redacted>" in result.output