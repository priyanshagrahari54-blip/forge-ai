"""Dynamic agent request envelope."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class AgentRequest:
    role:str
    capabilities:tuple=field(default_factory=tuple)
    quality:str="standard"
