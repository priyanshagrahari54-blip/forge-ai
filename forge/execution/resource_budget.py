"""Explicit resource budget for an execution attempt."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ResourceBudget:
    max_seconds:float=3600.0
    max_output_bytes:int=10_000_000
    max_actions:int=100
    def valid(self):return self.max_seconds>0 and self.max_output_bytes>0 and self.max_actions>0
