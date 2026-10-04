"""Task-graph execution context."""
from dataclasses import dataclass
@dataclass(frozen=True)
class DAGContext:
    task_id:str
    node_id:str
    dependencies:tuple=()
