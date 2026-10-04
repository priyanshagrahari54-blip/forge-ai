"""Structured failure object for truthful pipeline reporting."""
from dataclasses import dataclass
@dataclass(frozen=True)
class Failure:
    stage:str
    reason:str
    retryable:bool=False
