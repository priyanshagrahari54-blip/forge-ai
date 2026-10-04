"""Capability discovery, truth, and reusable integration registry."""

from .reality import CapabilityTruth, capability_snapshot, default_capabilities
from .registry import CapabilityCandidate, CapabilityRegistry, ReuseStrategy

__all__ = [
    "CapabilityCandidate", "CapabilityRegistry", "CapabilityTruth",
    "ReuseStrategy", "capability_snapshot", "default_capabilities",
]
