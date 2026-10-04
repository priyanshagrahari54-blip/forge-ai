"""Provider health snapshot used as routing input."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ProviderHealth:
    provider:str
    available:bool=True
    latency_score:float=.5
    quality_score:float=.5
    cost_score:float=.5
    privacy_score:float=.5
