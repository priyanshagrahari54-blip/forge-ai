"""Bounded system health snapshot."""
from dataclasses import dataclass
@dataclass(frozen=True)
class HealthSnapshot:
    ready:bool
    components:dict
    def failed(self):return [k for k,v in self.components.items() if not v]
