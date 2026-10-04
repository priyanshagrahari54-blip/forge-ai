"""Explicit memory scope to prevent cross-project leakage."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ContextScope:
    project_id:str
    session_id:str
    def matches(self,project_id,session_id):return self.project_id==project_id and self.session_id==session_id
