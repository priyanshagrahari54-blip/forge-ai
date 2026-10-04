"""Non-secret approval reference; actual authorization remains server-side."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ApprovalToken:
    token_id:str
    project_id:str
    session_id:str
