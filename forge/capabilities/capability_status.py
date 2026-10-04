"""Capability lifecycle states."""
from enum import Enum
class CapabilityStatus(str,Enum):
    DISCOVERED="discovered"; VERIFIED="verified"; BLOCKED="blocked"; ADOPTED="adopted"
