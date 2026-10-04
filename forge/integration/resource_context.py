"""Resource context without assuming external compute exists."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ResourceContext:
    cpu:int=0
    memory_mb:int=0
    network:bool=True
    remote_compute_available:bool=False
