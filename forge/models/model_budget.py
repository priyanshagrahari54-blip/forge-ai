"""Per-selection model budget metadata."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ModelBudget:
    max_tokens:int=0
    max_seconds:float=0.0
    max_cost:float=0.0
