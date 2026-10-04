"""Minimal artifact manifest for delivery traceability."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ArtifactManifest:
    path:str
    kind:str="file"
    checksum:str=""
    verified:bool=False
