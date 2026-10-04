"""Explicit agent capability gap representation."""
from dataclasses import dataclass
@dataclass(frozen=True)
class AgentGap:
    capability:str
    reason:str
    dynamic_creation_allowed:bool=True
