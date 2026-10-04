"""Resolve requirement capabilities into existing capability packs."""
from __future__ import annotations
from forge.capabilities.packs import CORE_PACKS
class CapabilityPackResolver:
    def resolve(self, capabilities):
        names={str(x).lower() for x in (capabilities or [])}
        out=[]
        for name,pack in CORE_PACKS.items():
            if name in names or any(name in n for n in names):out.append(pack)
        return out
