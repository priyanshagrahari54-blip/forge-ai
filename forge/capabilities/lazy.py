"""Lazy capability adoption: resolve first, install/execute only after approval."""
from __future__ import annotations
from dataclasses import dataclass
from .broker import CapabilityBroker, CapabilityResolution

@dataclass(frozen=True)
class LazyDecision:
    resolution: CapabilityResolution
    action: str
    requires_approval: bool

class LazyCapabilityLoader:
    def __init__(self, broker: CapabilityBroker): self.broker=broker
    def prepare(self, capability: str)->LazyDecision:
        r=self.broker.resolve(capability)
        if r.available:
            return LazyDecision(r,"reuse",False)
        return LazyDecision(r,"discover_then_verify",True)
    def prepare_many(self, capabilities):
        return [self.prepare(c) for c in dict.fromkeys(capabilities)]
