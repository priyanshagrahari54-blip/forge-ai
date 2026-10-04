"""Tool request envelope used before invocation."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class ToolRequest:
    name:str
    project_id:str
    session_id:str
    permissions:tuple=field(default_factory=tuple)
