"""Hardware engineering mode (A83).

Probes the real machine and reports support as SUPPORTED /
PARTIALLY_SUPPORTED / UNSUPPORTED / UNKNOWN. UNKNOWN is the default and is
never reported as UNSUPPORTED: "could not look" is not "will not work".
"""
from __future__ import annotations

from forge.hardware.agent import (
    CapabilityMatrix,
    HardwareAgent,
    SubsystemStatus,
    build_matrix,
    render,
)
from forge.hardware.probe import (
    PARTIALLY_SUPPORTED,
    STATUSES,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    Device,
    HardwareProbe,
    HardwareReport,
    pci_class_name,
)

__all__ = [
    "CapabilityMatrix", "Device", "HardwareAgent", "HardwareProbe",
    "HardwareReport", "PARTIALLY_SUPPORTED", "STATUSES", "SUPPORTED",
    "SubsystemStatus", "UNKNOWN", "UNSUPPORTED", "build_matrix",
    "pci_class_name", "render",
]
