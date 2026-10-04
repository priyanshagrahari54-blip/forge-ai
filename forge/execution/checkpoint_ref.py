"""Reference to an execution checkpoint."""
from dataclasses import dataclass
@dataclass(frozen=True)
class CheckpointRef:
    checkpoint_id:str
    task_id:str
    created_at:float=0.0
    def valid(self): return bool(self.checkpoint_id and self.task_id)
