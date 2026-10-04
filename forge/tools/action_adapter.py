"""Adapt tool metadata to the canonical action gateway."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ActionEnvelope:
    kind:str
    action:str
    project_id:str
    session_id:str
    risk:str="low"
    approved:bool=False
