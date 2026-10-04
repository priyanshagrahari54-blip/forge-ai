"""Minimal agent execution contract."""
from dataclasses import dataclass
@dataclass(frozen=True)
class AgentContract:
    role:str
    capabilities:tuple=()
    permissions:tuple=()
    quality_bar:str="standard"
    def valid(self):return bool(self.role)
