"""Project/session scope value object."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ProjectScope:
    project_id:str
    session_id:str
    def valid(self):return bool(self.project_id and self.session_id)
