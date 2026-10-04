"""Lightweight correlation context for pipeline events."""
from dataclasses import dataclass
@dataclass(frozen=True)
class TraceContext:
    trace_id:str
    task_id:str=""
    project_id:str=""
    def valid(self): return bool(self.trace_id)
