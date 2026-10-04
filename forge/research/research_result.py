"""Truthful research result envelope."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class ResearchResult:
    status:str
    findings:tuple=field(default_factory=tuple)
    sources:tuple=field(default_factory=tuple)
    error:str=""
