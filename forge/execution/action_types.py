"""Canonical executable action taxonomy."""
from enum import Enum
class ActionKind(str, Enum):
    FILE="file"; TERMINAL="terminal"; BROWSER="browser"; TOOL="tool"; NETWORK="network"; MODEL="model"
