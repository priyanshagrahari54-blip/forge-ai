"""Approval context for gated actions."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ApprovalContext:
    project_id:str
    session_id:str
    approved_actions:tuple=()
    def permits(self,action): return action in self.approved_actions
