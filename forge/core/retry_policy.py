"""Bounded retry policy for autonomous execution."""
from dataclasses import dataclass
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts:int=8
    def allowed(self,attempt): return 0 < attempt < self.max_attempts
