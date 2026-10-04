"""Bounded execution attempt identity."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ExecutionAttempt:
    task_id:str
    attempt:int=1
    def valid(self):return bool(self.task_id) and self.attempt>0
