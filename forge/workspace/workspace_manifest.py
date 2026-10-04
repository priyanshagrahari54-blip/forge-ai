"""Workspace manifest for project/session traceability."""
from dataclasses import dataclass
@dataclass(frozen=True)
class WorkspaceManifest:
    project_id:str
    session_id:str
    root:str
    def valid(self): return bool(self.project_id and self.session_id and self.root)
