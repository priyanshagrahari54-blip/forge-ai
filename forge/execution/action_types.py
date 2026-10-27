"""Canonical executable action taxonomy."""
from enum import Enum


class _CompatStrEnum(str, Enum):
    """Python 3.8-compatible string enum with _CompatStrEnum-style formatting."""
    def __str__(self):
        return self.value


class ActionKind(_CompatStrEnum):
    FILE = "file"
    TERMINAL = "terminal"
    BROWSER = "browser"
    TOOL = "tool"
    NETWORK = "network"
    MODEL = "model"
