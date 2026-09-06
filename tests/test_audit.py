from forge.security.audit import AuditLog


def fixed_clock(timestamps):
    timestamps = iter(timestamps)
    return lambda: next(timestamps)


def test_record_entry_fields():
    log = AuditLog(clock=fixed_clock(["2026-09-06T00:00:00+00:00"]))

    entry = log.record(
        "write_file",
        level="warning",
        actor="tester",
        target="secret.txt",
        result="denied",
        message="approval required",
    )

    assert entry.timestamp == "2026-09-06T00:00:00+00:00"
    assert entry.operation == "write_file"
    assert entry.level == "warning"
    assert entry.actor == "tester"
    assert entry.target == "secret.txt"
    assert entry.result == "denied"
    assert entry.message == "approval required"


def test_record_redacts_secrets_from_message():
    log = AuditLog()

    entry = log.record(
        "read_file",
        message="content=AKIAIOSFODNN7EXAMPLE",
    )

    assert "AKIAIOSFODNN7EXAMPLE" not in entry.message
    assert "<redacted>" in entry.message


def test_recent_returns_most_recent():
    log = AuditLog()

    for index in range(5):
        log.record(
            "read_file",
            message=f"entry-{index}",
        )

    recent = log.recent(2)

    assert [entry.message for entry in recent] == ["entry-3", "entry-4"]


def test_by_operation_filters():
    log = AuditLog()

    log.record("read_file", message="a")
    log.record("write_file", message="b")
    log.record("read_file", message="c")

    operations = log.by_operation("read_file")

    assert [entry.message for entry in operations] == ["a", "c"]


def test_clear_empties_log():
    log = AuditLog()

    log.record("read_file", message="a")

    log.clear()

    assert log.entries == ()


def test_max_entries_is_bounded():
    log = AuditLog(max_entries=3)

    for index in range(10):
        log.record("read_file", message=f"entry-{index}")

    entries = log.entries

    assert len(entries) == 3
    assert [entry.message for entry in entries] == [
        "entry-7",
        "entry-8",
        "entry-9",
    ]


def test_recent_requires_non_negative_limit():
    import pytest

    log = AuditLog()

    with pytest.raises(ValueError):
        log.recent(-1)