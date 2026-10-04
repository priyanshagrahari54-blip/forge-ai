"""Execution scope for one work unit."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ExecutionScope:
    project_id:str
    session_id:str
    task_id:str
    def valid(self): return bool(self.project_id and self.session_id and self.task_id)
