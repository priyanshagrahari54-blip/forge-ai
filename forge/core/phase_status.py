"""Canonical phase states for pipeline observability."""
from enum import Enum
class PhaseStatus(str, Enum):
    PENDING="PENDING"; RUNNING="RUNNING"; PASSED="PASSED"; FAILED="FAILED"; BLOCKED="BLOCKED"
