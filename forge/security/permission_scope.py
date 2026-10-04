"""Project-scoped permission value object."""
from dataclasses import dataclass
@dataclass(frozen=True)
class PermissionScope:
    project_id:str
    session_id:str
    permissions:tuple=()
    def allows(self,permission):return permission in self.permissions
