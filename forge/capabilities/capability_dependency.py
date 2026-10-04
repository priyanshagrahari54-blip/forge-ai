"""Capability dependency declaration."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class CapabilityDependency:
    name:str
    required:tuple=field(default_factory=tuple)
