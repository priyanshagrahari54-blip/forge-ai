from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from forge.security.secrets import SecretRedactor


def default_clock() -> str:
    """Return the current UTC time as an ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditEntry:
    """An immutable entry in the Forge audit trail.

    ``message`` is guaranteed redacted: detected secrets are replaced
    with a fixed marker before the entry is stored.
    """

    timestamp: str
    level: str
    operation: str
    actor: str = ""
    target: str = ""
    result: str = ""
    message: str = ""


class AuditLog:
    """Bounded, in-memory audit trail with mandatory secret redaction.

    The clock is injectable so tests can stay deterministic, but the
    default is a UTC ``datetime`` timestamp.
    """

    DEFAULT_MAX_ENTRIES = 1000

    def __init__(
        self,
        clock: Callable[[], str] | None = None,
        redactor: SecretRedactor | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.clock = clock or default_clock
        self.redactor = redactor or SecretRedactor()
        self.max_entries = max_entries
        self._entries: list[AuditEntry] = []

    def record(
        self,
        operation: str,
        *,
        level: str = "info",
        actor: str = "",
        target: str = "",
        result: str = "",
        message: str = "",
    ) -> AuditEntry:
        """Record an audited operation, redacting any secret in message."""
        safe_message, _findings = self.redactor.redact(message)

        entry = AuditEntry(
            timestamp=self.clock(),
            level=level,
            operation=operation,
            actor=actor,
            target=target,
            result=result,
            message=safe_message,
        )

        self._entries.append(entry)

        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]

        return entry

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def recent(self, limit: int = 10) -> tuple[AuditEntry, ...]:
        """Return the most recent entries in chronological order."""
        if limit < 0:
            raise ValueError("limit must be non-negative")

        return tuple(self._entries[-limit:])

    def by_operation(self, operation: str) -> tuple[AuditEntry, ...]:
        return tuple(
            entry
            for entry in self._entries
            if entry.operation == operation
        )

    def clear(self) -> None:
        self._entries.clear()