"""QEMU / VM testing (A83).

Source → build → boot → judge from the serial console. A boot is only
reported successful when the guest printed its readiness marker; a missing
QEMU is ``unavailable``, never ``booted``.
"""
from __future__ import annotations

from forge.vm.qemu import (
    BOOTED,
    NO_IMAGE,
    NO_MARKER,
    PANIC,
    TIMEOUT,
    UNAVAILABLE,
    VERDICTS,
    BootReport,
    BootResult,
    BootScenario,
    BootTestAgent,
    VmHarness,
    judge,
    render,
)

__all__ = [
    "BOOTED", "BootReport", "BootResult", "BootScenario", "BootTestAgent",
    "NO_IMAGE", "NO_MARKER", "PANIC", "TIMEOUT", "UNAVAILABLE", "VERDICTS",
    "VmHarness", "judge", "render",
]
