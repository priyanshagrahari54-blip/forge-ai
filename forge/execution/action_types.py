"""Canonical executable action taxonomy."""
try:
    from enum import StrEnum
except ImportError:  # Python 3.8 compatibility
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self):
            return self.value
class ActionKind(StrEnum):
    FILE="file"; TERMINAL="terminal"; BROWSER="browser"; TOOL="tool"; NETWORK="network"; MODEL="model"
