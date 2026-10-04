"""Single bounded context envelope for pipeline stages."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class PipelineContext:
    task_id:str
    project_id:str
    session_id:str
    phase:str="PENDING"
    data:dict=field(default_factory=dict)
