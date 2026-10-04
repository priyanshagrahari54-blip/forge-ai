"""Audit record for high-impact actions."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ActionAudit:
    action:str
    project_id:str
    session_id:str
    approved:bool
    reason:str=""
