"""Stable project/session identity boundary."""
from dataclasses import dataclass
@dataclass(frozen=True)
class SessionScope:
    project_id:str
    session_id:str
    def valid(self): return bool(self.project_id and self.session_id)
