"""Project-scoped provenance record."""
from dataclasses import dataclass
@dataclass(frozen=True)
class Provenance:
    source:str
    project_id:str
    kind:str="unknown"
    trusted:bool=False
