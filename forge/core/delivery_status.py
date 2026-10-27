"""Truthful final delivery states."""
from enum import Enum


class _CompatStrEnum(str, Enum):
    """Python 3.8-compatible string enum with _CompatStrEnum-style formatting."""
    def __str__(self):
        return self.value


class DeliveryStatus(_CompatStrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
