"""Normalized test result."""
from dataclasses import dataclass
@dataclass(frozen=True)
class TestResult:
    name:str
    status:str
    details:str=""
