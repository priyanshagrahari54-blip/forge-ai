"""Capability discovery, truth, and reusable integration registry."""

from .adoption import AdoptionDecision, AdoptionPolicy, decide, rank as adoption_rank, verify_candidate
from .broker import CapabilityBroker, CapabilityResolution
from .package_discovery import discover_npm, discover_pypi
from .ranking import best, rank
from .reality import CapabilityTruth, capability_snapshot, default_capabilities
from .registry import CapabilityCandidate, CapabilityRegistry, ReuseStrategy

__all__ = [
    "AdoptionDecision", "AdoptionPolicy", "adoption_rank", "decide", "verify_candidate",
    "CapabilityBroker", "CapabilityCandidate", "CapabilityRegistry",
    "CapabilityResolution", "CapabilityTruth", "ReuseStrategy",
    "capability_snapshot", "default_capabilities",
    "discover_npm", "discover_pypi", "rank", "best",
]
