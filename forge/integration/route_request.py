"""Stable routing request across agent/model selection."""
from dataclasses import dataclass
@dataclass(frozen=True)
class RouteRequest:
    task_type:str
    capability:str="text"
    quality:float=.5
    latency:float=.5
    cost:float=.5
    privacy:bool=False
