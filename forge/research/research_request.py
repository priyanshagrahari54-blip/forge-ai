"""Bounded research request envelope."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class ResearchRequest:
    query:str
    project_id:str
    sources:tuple=field(default_factory=tuple)
    max_results:int=10
    def valid(self): return bool(self.query and self.project_id and self.max_results>0)
