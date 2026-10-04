"""Project manifest identity."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ProjectManifest:
    project_id:str
    root:str
    profile:str="default"
    def valid(self): return bool(self.project_id and self.root)
