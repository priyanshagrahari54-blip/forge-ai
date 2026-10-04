"""Stable capability request object."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class CapabilityRequest:
    names:tuple=field(default_factory=tuple)
    quality:str="standard"
    allow_discovery:bool=True
