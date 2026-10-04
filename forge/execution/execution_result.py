"""Truthful execution result envelope."""
from dataclasses import dataclass, field
@dataclass(frozen=True)
class ExecutionResult:
    status:str
    evidence:tuple=field(default_factory=tuple)
    error:str=""
    def successful(self): return self.status=="PASS" and bool(self.evidence)
