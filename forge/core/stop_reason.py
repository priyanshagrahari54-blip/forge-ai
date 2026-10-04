"""Explicit reasons for stopping a run."""
from enum import Enum
class StopReason(str,Enum):
    COMPLETED="completed"; FAILED="failed"; BLOCKED="blocked"; CANCELLED="cancelled"; DEADLINE="deadline"
