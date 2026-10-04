"""Compact task context shared across planner, agents and execution."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class TaskContext:
    task_id:str
    project_id:str
    session_id:str
    requirement:str=""
    capabilities:tuple=field(default_factory=tuple)
    def valid(self): return bool(self.task_id and self.project_id and self.session_id)
