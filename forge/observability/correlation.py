"""Execution correlation identifiers for telemetry."""
from dataclasses import dataclass
import uuid
@dataclass(frozen=True)
class Correlation:
    run_id:str
    task_id:str
    @classmethod
    def create(cls,task_id:str):return cls(uuid.uuid4().hex,task_id)
