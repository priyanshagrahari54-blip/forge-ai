"""Evidence context passed into quality gates."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class EvidenceContext:
    items:tuple=field(default_factory=tuple)
    source_count:int=0
    def has_evidence(self): return bool(self.items)
