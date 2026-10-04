"""Explicit capability gap representation."""
from dataclasses import dataclass
@dataclass(frozen=True)
class CapabilityGap:
    name:str
    reason:str
    can_discover:bool=True
