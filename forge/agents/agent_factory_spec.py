"""Data-only dynamic agent specification."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class AgentFactorySpec:
    role:str
    capabilities:tuple=field(default_factory=tuple)
    tools:tuple=field(default_factory=tuple)
    permissions:tuple=field(default_factory=tuple)
    model:str=""
