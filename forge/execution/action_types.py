"""Canonical executable action taxonomy."""
from enum import StrEnum
class ActionKind(StrEnum):
    FILE="file"; TERMINAL="terminal"; BROWSER="browser"; TOOL="tool"; NETWORK="network"; MODEL="model"
