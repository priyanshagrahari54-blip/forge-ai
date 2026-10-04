"""Provider-neutral model request envelope."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ModelRequest:
    capability:str="text"
    quality:float=.5
    privacy:bool=False
