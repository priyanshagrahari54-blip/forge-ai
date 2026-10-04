"""Truthful final delivery states."""
from enum import StrEnum
class DeliveryStatus(StrEnum): READY="READY"; BLOCKED="BLOCKED"; FAILED="FAILED"; UNKNOWN="UNKNOWN"
