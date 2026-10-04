"""Generic pipeline outcome with explicit evidence state."""
from dataclasses import dataclass
@dataclass(frozen=True)
class Outcome:
    status:str
    value:object=None
    evidence:tuple=()
    error:str=""
