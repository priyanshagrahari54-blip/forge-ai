"""Explicit model availability gap; never fabricate a model."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ModelGap:
    capability:str
    reason:str
    discoverable:bool=True
