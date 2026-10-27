"""Canonical phase states for pipeline observability."""
from enum import Enum


class StrEnum(str, Enum):
    """Python 3.8-compatible string enum with StrEnum-style formatting."""
    def __str__(self):
        return self.value


class PhaseStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
