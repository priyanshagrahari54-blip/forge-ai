"""Capability discovery, truth, and reusable integration registry."""

from .broker import CapabilityBroker, CapabilityResolution
from .package_discovery import discover_npm, discover_pypi
from .ranking import best, rank
from .reality import CapabilityTruth, capability_snapshot, default_capabilities
from .registry import CapabilityCandidate, CapabilityRegistry, ReuseStrategy

__all__ = [
    "CapabilityBroker", "CapabilityCandidate", "CapabilityRegistry",
    "CapabilityResolution", "CapabilityTruth", "ReuseStrategy",
    "capability_snapshot", "default_capabilities",
    "discover_npm", "discover_pypi", "rank", "best",
]
