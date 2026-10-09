"""Truthful final delivery states."""
from enum import Enum
class DeliveryStatus(str, Enum): READY="READY"; BLOCKED="BLOCKED"; FAILED="FAILED"; UNKNOWN="UNKNOWN"
