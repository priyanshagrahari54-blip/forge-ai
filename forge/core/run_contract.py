"""Run-level contract linking requirement and delivery evidence."""
from dataclasses import dataclass, field
@dataclass
class RunContract:
    requirement_id:str
    acceptance:tuple=field(default_factory=tuple)
    evidence:tuple=field(default_factory=tuple)
    def ready(self): return bool(self.requirement_id and self.acceptance)
