"""Atomic execution work unit."""
from dataclasses import dataclass
@dataclass(frozen=True)
class WorkUnit:
    task_id:str
    node_id:str
    action:str
    def valid(self): return bool(self.task_id and self.node_id and self.action)
