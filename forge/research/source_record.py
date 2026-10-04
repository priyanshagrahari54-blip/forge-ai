"""Normalized research source record with provenance."""
from dataclasses import dataclass
@dataclass(frozen=True)
class SourceRecord:
    uri:str
    title:str=""
    publisher:str=""
    retrieved_at:float=0.0
    trusted:bool=False
