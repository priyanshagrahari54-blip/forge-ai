"""Canonical executable action taxonomy."""
from enum import Enum


class StrEnum(str, Enum):
    """Python 3.8-compatible string enum with StrEnum-style formatting."""
    def __str__(self):
        return self.value


class ActionKind(StrEnum):
    FILE = "file"
    TERMINAL = "terminal"
    BROWSER = "browser"
    TOOL = "tool"
    NETWORK = "network"
    MODEL = "model"
