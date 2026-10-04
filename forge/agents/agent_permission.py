"""Agent permission envelope."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class AgentPermission:
    agent_id:str
    permissions:tuple=field(default_factory=tuple)
    def allows(self,permission): return permission in self.permissions
