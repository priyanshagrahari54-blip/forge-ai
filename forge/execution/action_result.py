"""Action result with explicit status."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ActionResult:
    action:str
    status:str
    output:object=None
    error:str=""
