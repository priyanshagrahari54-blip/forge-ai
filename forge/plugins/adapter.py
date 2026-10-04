"""Safe plugin adapter contract. Discovery is separate from loading."""
from dataclasses import dataclass
from typing import Any,Protocol
@dataclass(frozen=True)
class PluginManifest:
    name:str
    version:str
    capabilities:tuple[str,...]
    entrypoint:str=""
    permissions:tuple[str,...]=()
    verified:bool=False
class PluginAdapter(Protocol):
    def manifest(self)->PluginManifest: ...
    def health(self)->dict[str,Any]: ...
    def invoke(self,operation:str,payload:dict[str,Any])->dict[str,Any]: ...
def can_load(manifest:PluginManifest,approved_permissions:set[str])->tuple[bool,str]:
    if not manifest.verified:return False,"plugin is unverified"
    if not set(manifest.permissions)<=approved_permissions:return False,"permissions exceed approval"
    if not manifest.entrypoint:return False,"missing entrypoint"
    return True,"approved"
