"""Execution modes."""
from enum import Enum
class RunMode(str,Enum):
    MANUAL="manual"
    ASSISTED="assisted"
    AUTONOMOUS="autonomous"
