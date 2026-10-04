"""Approval scope for high-impact actions."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class ApprovalScope:
    project_id:str
    session_id:str
    actions:tuple=field(default_factory=tuple)
    expires_at:float=0.0
    def permits(self,action): return action in self.actions
