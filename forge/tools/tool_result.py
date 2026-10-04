"""Tool result envelope that preserves failure truth."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ToolResult:
    name:str
    status:str
    output:object=None
    error:str=""
