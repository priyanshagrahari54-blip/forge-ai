"""Canonical phase states for pipeline observability."""
from enum import Enum


class _CompatStrEnum(str, Enum):
    """Python 3.8-compatible string enum with _CompatStrEnum-style formatting."""
    def __str__(self):
        return self.value


class PhaseStatus(_CompatStrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
