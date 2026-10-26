"""Canonical phase states for pipeline observability."""
try:
    from enum import StrEnum
except ImportError:  # Python 3.8 compatibility
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self):
            return self.value
class PhaseStatus(StrEnum):
    PENDING="PENDING"; RUNNING="RUNNING"; PASSED="PASSED"; FAILED="FAILED"; BLOCKED="BLOCKED"
