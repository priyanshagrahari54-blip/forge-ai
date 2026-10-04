"""Declarative universal domain capability packs."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CapabilityPack:
    name:str
    capabilities:tuple[str,...]
    preferred_tools:tuple[str,...]=()
    preferred_roles:tuple[str,...]=()
    quality_bar:str="standard"

CORE_PACKS=(
    CapabilityPack("software",("web","backend","frontend","testing","git"),("terminal","filesystem"),("engineer","tester")),
    CapabilityPack("research",("web-research","retrieval","citation"),("browser","search"),("researcher",)),
    CapabilityPack("creative",("image","video","audio","vfx","3d"),("creative-tools",),("creative",),"professional"),
    CapabilityPack("game",("game-development","3d","asset-pipeline","testing"),("engine","blender"),("engineer","creative"),"high"),
    CapabilityPack("os",("kernel","systems","boot","virtualization","testing"),("compiler","qemu"),("systems","tester"),"high"),
    CapabilityPack("ai",("models","rag","fine-tuning","evaluation"),("huggingface",),("researcher","engineer"),"high"),
)

def get_pack(name:str)->CapabilityPack|None:
    return next((p for p in CORE_PACKS if p.name==name),None)
