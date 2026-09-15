"""Hardware agent (A83): probe, classify, and refuse to invent.

The agent turns a raw probe into the answer a project actually needs: *what
does this machine have, and what do we support on it?* Support is decided by
evidence, and the decision is recorded with the evidence attached.

The default is :data:`~forge.hardware.probe.UNKNOWN`. A device only becomes
``UNSUPPORTED`` when it was **observed** and no driver is registered for it —
a positive statement about a real device. A device Forge could not see stays
``UNKNOWN``, because "could not look" is not "will not work".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.hardware.probe import (
    PARTIALLY_SUPPORTED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    Device,
    HardwareProbe,
    HardwareReport,
)

#: Categories a ZEROOS-style project cares about, mapped to the probe
#: categories that answer them.
SUBSYSTEMS: Dict[str, Tuple[str, ...]] = {
    "cpu": ("cpu",),
    "memory": ("memory",),
    "storage": ("storage",),
    "network": ("network",),
    "display": ("display", "pci"),
    "audio": ("audio",),
    "input": ("input",),
    "usb": ("usb",),
    "pci": ("pci",),
    "firmware": ("firmware",),
}


@dataclass
class SubsystemStatus:
    """Support status for one subsystem, with the evidence behind it."""

    subsystem: str
    status: str
    observed: int
    supported: int
    evidence: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"subsystem": self.subsystem, "status": self.status,
                "observed": self.observed, "supported": self.supported,
                "evidence": list(self.evidence)[:20], "reason": self.reason}


@dataclass
class CapabilityMatrix:
    """The honest answer to "what does this build support here?"."""

    subsystems: List[SubsystemStatus] = field(default_factory=list)
    device_count: int = 0
    tool_inventory: Dict[str, bool] = field(default_factory=dict)

    def status(self, subsystem: str) -> str:
        for item in self.subsystems:
            if item.subsystem == subsystem:
                return item.status
        return UNKNOWN

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for item in self.subsystems:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "subsystems": [item.to_dict() for item in self.subsystems],
            "device_count": self.device_count,
            "status_counts": counts,
            "tool_inventory": dict(self.tool_inventory),
        }


def build_matrix(report: HardwareReport, *,
                 tools: Optional[Dict[str, bool]] = None,
                 drivers: Sequence[str] = ()) -> CapabilityMatrix:
    """Derive subsystem support from observed devices.

    ``drivers`` is the set of driver names the project claims to implement.
    A device whose bound driver is in that set is SUPPORTED; a device that is
    observed but has no driver in the set is UNSUPPORTED; a device that could
    not be observed keeps its UNKNOWN status.
    """
    known = {name for name in drivers if name}
    matrix = CapabilityMatrix(
        device_count=len(report.devices),
        tool_inventory=dict(tools if tools is not None else {}))
    for subsystem, categories in sorted(SUBSYSTEMS.items()):
        observed: List[Device] = []
        for category in categories:
            observed.extend(report.by_category(category))
        if not observed:
            matrix.subsystems.append(SubsystemStatus(
                subsystem=subsystem, status=UNKNOWN, observed=0, supported=0,
                reason="no device in this category could be probed on this "
                       "machine"))
            continue
        supported: List[Device] = []
        for device in observed:
            if device.status != SUPPORTED:
                continue
            if known and device.label and device.label not in known:
                continue
            supported.append(device)
        if supported:
            status = SUPPORTED if len(supported) == len(
                [item for item in observed if item.status == SUPPORTED]
            ) else PARTIALLY_SUPPORTED
            reason = ("%d of %d observed device(s) are supported"
                      % (len(supported), len(observed)))
        else:
            status = UNSUPPORTED
            reason = ("%d device(s) observed, none supported"
                      % len(observed))
        matrix.subsystems.append(SubsystemStatus(
            subsystem=subsystem, status=status, observed=len(observed),
            supported=len(supported),
            evidence=[item.evidence[0] for item in observed[:20]
                      if item.evidence],
            reason=reason))
    return matrix


def render(matrix: CapabilityMatrix) -> str:
    """Human-readable support matrix — the text a reviewer reads."""
    lines = ["hardware: %d device(s) observed" % matrix.device_count]
    for item in matrix.subsystems:
        lines.append("  %-10s %-20s %d/%d — %s" % (
            item.subsystem, item.status, item.supported, item.observed,
            item.reason))
    missing = sorted(name for name, present in matrix.tool_inventory.items()
                     if not present)
    if missing:
        lines.append("  tools absent here: %s" % ", ".join(missing))
    return "\n".join(lines)


class HardwareAgent:
    """Probe agent exposing the hardware matrix as a Forge agent executor."""

    __test__ = False
    name = "hardware"
    role = "hardware"

    def __init__(self, root: str | Path = ".", *, profile: Any = None,
                 probe: Optional[HardwareProbe] = None,
                 drivers: Sequence[str] = ()) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.probe = probe or HardwareProbe()
        self.drivers = tuple(drivers)

    def describe(self) -> str:
        return ("Probes the machine and reports SUPPORTED / "
                "PARTIALLY_SUPPORTED / UNSUPPORTED / UNKNOWN per subsystem, "
                "with the evidence for each status.")

    def attach(self, context: Any) -> "HardwareAgent":
        self.context = context
        return self

    def survey(self) -> Tuple[HardwareReport, CapabilityMatrix]:
        report = self.probe.probe()
        matrix = build_matrix(report, tools=self.probe.tool_inventory(),
                              drivers=self.drivers)
        return report, matrix

    def execute(self, request: Any) -> Any:
        from forge.agents.execution import AgentResponse
        report, matrix = self.survey()
        context = getattr(self, "context", None)
        if context is not None:
            context.record("hardware", "hardware", matrix.summary(),
                           source="agent:hardware",
                           note="probed on the running machine")
            context.note_turn(
                self.name, self.role,
                "probed %d device(s)" % matrix.device_count,
                ok=True, devices=matrix.device_count)
        # A probe is only "successful" when it actually observed something.
        ok = matrix.device_count > 0
        # ``request`` may be absent when the agent is driven directly rather
        # than through the pipeline; a survey is still a survey.
        context_obj = getattr(request, "context", None) if request else None
        return AgentResponse(
            success=ok, output=render(matrix), agent=self.name,
            stage=getattr(request, "stage", "") if request else "",
            error="" if ok else (
                "no hardware could be probed on this machine; every status "
                "is UNKNOWN"),
            context_fingerprint=(
                getattr(context_obj, "fingerprint", "") if context_obj else ""),
            metadata={"devices": str(matrix.device_count),
                      "unknown": str(sum(
                          1 for item in matrix.subsystems
                          if item.status == UNKNOWN))})
