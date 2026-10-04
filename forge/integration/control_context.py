"""Control-plane context shared by orchestration stages."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ControlContext:
    run_id:str
    project_id:str
    session_id:str
    mode:str="manual"
