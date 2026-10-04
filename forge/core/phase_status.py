"""Canonical phase states for pipeline observability."""
from enum import StrEnum
class PhaseStatus(StrEnum):
    PENDING="PENDING"; RUNNING="RUNNING"; PASSED="PASSED"; FAILED="FAILED"; BLOCKED="BLOCKED"
