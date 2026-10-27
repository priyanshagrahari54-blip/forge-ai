"""Truthful final delivery states."""
from enum import Enum


class StrEnum(str, Enum):
    """Python 3.8-compatible string enum with StrEnum-style formatting."""
    def __str__(self):
        return self.value


class DeliveryStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
