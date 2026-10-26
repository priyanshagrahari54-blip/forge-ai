"""Truthful final delivery states."""
try:
    from enum import StrEnum
except ImportError:  # Python 3.8 compatibility
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self):
            return self.value

class DeliveryStatus(StrEnum): READY="READY"; BLOCKED="BLOCKED"; FAILED="FAILED"; UNKNOWN="UNKNOWN"
