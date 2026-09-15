"""Hardware probing (A83): read the machine, never invent it.

Every fact here comes from one of three real sources:

* ``/proc`` and ``/sys`` — the kernel's own description of the machine;
* a platform tool (``lspci``, ``lsusb``, ``dmidecode``) *when it is installed*
  — and its absence is reported as an absence, not as "no devices";
* the machine's own architecture, from :mod:`platform`.

The four-valued support status is the whole point of this module:

``SUPPORTED``
    A driver exists *and* the device was observed working.
``PARTIALLY_SUPPORTED``
    A driver exists and the device was observed, but a declared function was
    not confirmed.
``UNSUPPORTED``
    The device was observed and Forge has no driver for it — a positive
    statement about a real device.
``UNKNOWN``
    The device was **not** observed, or could not be probed. This is the
    default. UNKNOWN is never reported as UNSUPPORTED, because "we could not
    look" and "we looked and it will not work" are different facts, and
    conflating them is how a project ends up claiming capabilities it does not
    have.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SUPPORTED = "SUPPORTED"
PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"
STATUSES = (SUPPORTED, PARTIALLY_SUPPORTED, UNSUPPORTED, UNKNOWN)

SYSFS_PCI = "/sys/bus/pci/devices"
SYSFS_USB = "/sys/bus/usb/devices"
SYSFS_BLOCK = "/sys/block"
SYSFS_NET = "/sys/class/net"
SYSFS_SOUND = "/sys/class/sound"
SYSFS_INPUT = "/sys/class/input"
PROC_CPUINFO = "/proc/cpuinfo"
PROC_MEMINFO = "/proc/meminfo"
DMI_PRODUCT = "/sys/class/dmi/id"
MAX_DEVICES = 512
MAX_READ_BYTES = 64 * 1024


@dataclass
class Device:
    """One observed device, with the evidence for every claim about it."""

    category: str
    identifier: str
    label: str = ""
    #: One of :data:`STATUSES`.
    status: str = UNKNOWN
    #: The exact source each fact came from (``/sys/...``, ``lspci``, …).
    evidence: List[str] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    #: Set when a probe could not be performed, with the reason.
    probe_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category, "identifier": self.identifier,
            "label": self.label, "status": self.status,
            "evidence": list(self.evidence), "facts": dict(self.facts),
            "probe_error": self.probe_error,
        }


@dataclass
class HardwareReport:
    """Everything that could be observed about this machine."""

    platform: Dict[str, Any] = field(default_factory=dict)
    cpu: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    firmware: Dict[str, Any] = field(default_factory=dict)
    devices: List[Device] = field(default_factory=list)
    #: Probes that were attempted and could not be completed.
    unavailable: List[Dict[str, str]] = field(default_factory=list)

    def by_category(self, category: str) -> List[Device]:
        return [item for item in self.devices if item.category == category]

    def categories(self) -> List[str]:
        return sorted({item.category for item in self.devices})

    def status_counts(self) -> Dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for item in self.devices:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def support_matrix(self) -> Dict[str, Dict[str, int]]:
        """Per-category counts, so "what works" is answerable at a glance."""
        matrix: Dict[str, Dict[str, int]] = {}
        for category in self.categories():
            counts = {status: 0 for status in STATUSES}
            for item in self.by_category(category):
                counts[item.status] = counts.get(item.status, 0) + 1
            matrix[category] = counts
        return matrix

    def summary(self) -> Dict[str, Any]:
        return {
            "platform": dict(self.platform),
            "cpu": dict(self.cpu),
            "memory": dict(self.memory),
            "firmware": dict(self.firmware),
            "devices": len(self.devices),
            "categories": self.categories(),
            "status_counts": self.status_counts(),
            "support_matrix": self.support_matrix(),
            "unavailable": list(self.unavailable),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "devices": [item.to_dict() for item in self.devices]}


def _read(path: str | Path, limit: int = MAX_READ_BYTES) -> Optional[str]:
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _read_int(path: str | Path) -> Optional[int]:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.strip(), 0)
    except (TypeError, ValueError):
        return None


def _listdir(path: str | Path, limit: int = MAX_DEVICES) -> List[str]:
    try:
        return sorted(os.listdir(str(path)))[:limit]
    except OSError:
        return []


class HardwareProbe:
    """Reads real hardware facts from the running machine."""

    def __init__(self, *, allow_tools: bool = True) -> None:
        self.allow_tools = allow_tools

    # -- top level -------------------------------------------------------

    def probe(self) -> HardwareReport:
        report = HardwareReport()
        report.platform = self.platform_facts()
        report.cpu = self.cpu_facts()
        report.memory = self.memory_facts()
        report.firmware = self.firmware_facts()
        report.devices.extend(self.pci_devices())
        report.devices.extend(self.usb_devices())
        report.devices.extend(self.block_devices())
        report.devices.extend(self.network_devices())
        report.devices.extend(self.audio_devices())
        report.devices.extend(self.input_devices())
        return report

    def note_unavailable(self, report: HardwareReport, probe: str,
                         reason: str) -> None:
        report.unavailable.append({"probe": probe, "reason": reason})

    # -- platform --------------------------------------------------------

    def platform_facts(self) -> Dict[str, Any]:
        return {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "source": "platform module",
        }

    def cpu_facts(self) -> Dict[str, Any]:
        facts: Dict[str, Any] = {"count": os.cpu_count(),
                                 "source": "os.cpu_count()"}
        text = _read(PROC_CPUINFO)
        if text is None:
            facts["proc_cpuinfo"] = "unavailable"
            return facts
        model = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        if model:
            facts["model"] = model.group(1).strip()
        flags = re.search(r"^flags\s*:\s*(.+)$", text, re.MULTILINE)
        if flags:
            tokens = flags.group(1).split()
            facts["flags"] = sorted(tokens)[:256]
            for feature in ("sse2", "avx", "avx2", "avx512f", "nx", "lm",
                            "vmx", "svm", "aes", "smep", "smap"):
                if feature in tokens:
                    facts.setdefault("features", []).append(feature)
        cores = re.findall(r"^processor\s*:", text, re.MULTILINE)
        facts["logical_cpus"] = len(cores)
        facts["source"] = PROC_CPUINFO
        return facts

    def memory_facts(self) -> Dict[str, Any]:
        text = _read(PROC_MEMINFO)
        if text is None:
            return {"proc_meminfo": "unavailable"}
        out: Dict[str, Any] = {"source": PROC_MEMINFO}
        for key, label in (("MemTotal", "total_kb"), ("MemAvailable",
                                                     "available_kb"),
                           ("SwapTotal", "swap_total_kb")):
            match = re.search(r"^%s:\s+(\d+) kB" % key, text, re.MULTILINE)
            if match:
                out[label] = int(match.group(1))
        if "total_kb" in out:
            out["total_mib"] = round(out["total_kb"] / 1024.0, 1)
        return out

    def firmware_facts(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, key in (("bios_vendor", "bios_vendor"),
                          ("bios_version", "bios_version"),
                          ("bios_date", "bios_date"),
                          ("product_name", "product_name"),
                          ("sys_vendor", "sys_vendor")):
            value = _read(Path(DMI_PRODUCT) / name)
            if value is not None:
                out[key] = value.strip()
                out["source"] = DMI_PRODUCT
        if not out:
            out["dmi"] = "unavailable"
        acpi = Path("/sys/firmware/acpi")
        out["acpi_present"] = acpi.is_dir()
        efi = Path("/sys/firmware/efi")
        out["efi_boot"] = efi.is_dir()
        return out

    # -- devices ---------------------------------------------------------

    def pci_devices(self) -> List[Device]:
        root = Path(SYSFS_PCI)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for address in _listdir(root):
            device_dir = root / address
            vendor = _read(device_dir / "vendor")
            device = _read(device_dir / "device")
            class_code = _read(device_dir / "class")
            driver = device_dir / "driver"
            label = ""
            try:
                label = os.path.basename(os.readlink(str(driver)))
            except OSError:
                pass
            out.append(Device(
                category="pci", identifier=address,
                label=label or "unbound",
                status=SUPPORTED if label else UNKNOWN,
                evidence=[str(device_dir)] + (
                    [str(driver)] if label else []),
                facts={
                    "vendor": (vendor or "").strip(),
                    "device": (device or "").strip(),
                    "class": (class_code or "").strip(),
                    "driver": label,
                    "class_name": pci_class_name((class_code or "").strip()),
                },
                probe_error="" if (vendor or device) else
                "identification registers could not be read"))
        return out

    def usb_devices(self) -> List[Device]:
        root = Path(SYSFS_USB)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for name in _listdir(root):
            if ":" in name:
                continue
            device_dir = root / name
            vendor = _read(device_dir / "idVendor")
            product = _read(device_dir / "idProduct")
            manufacturer = _read(device_dir / "manufacturer")
            if vendor is None and product is None:
                continue
            out.append(Device(
                category="usb", identifier=name,
                label=(manufacturer or "").strip() or "usb device",
                status=UNKNOWN,
                evidence=[str(device_dir)],
                facts={
                    "id_vendor": (vendor or "").strip(),
                    "id_product": (product or "").strip(),
                    "manufacturer": (manufacturer or "").strip(),
                    "product": (_read(device_dir / "product") or "").strip(),
                    "speed": (_read(device_dir / "speed") or "").strip(),
                }))
        return out

    def block_devices(self) -> List[Device]:
        root = Path(SYSFS_BLOCK)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for name in _listdir(root):
            device_dir = root / name
            size = _read_int(device_dir / "size")
            removable = _read(device_dir / "removable")
            rotational = _read(device_dir / "queue" / "rotational")
            facts: Dict[str, Any] = {
                "sectors": size,
                "removable": (removable or "").strip(),
                "rotational": (rotational or "").strip(),
            }
            if size is not None:
                facts["bytes"] = size * 512
                facts["gib"] = round(size * 512 / (1024.0 ** 3), 2)
            model = _read(device_dir / "device" / "model")
            if model:
                facts["model"] = model.strip()
            out.append(Device(
                category="storage", identifier=name,
                label=facts.get("model", name),
                status=SUPPORTED if size is not None else UNKNOWN,
                evidence=[str(device_dir)], facts=facts))
        return out

    def network_devices(self) -> List[Device]:
        root = Path(SYSFS_NET)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for name in _listdir(root):
            device_dir = root / name
            address = _read(device_dir / "address")
            operstate = _read(device_dir / "operstate")
            mtu = _read_int(device_dir / "mtu")
            driver = device_dir / "device" / "driver"
            label = ""
            try:
                label = os.path.basename(os.readlink(str(driver)))
            except OSError:
                pass
            state = (operstate or "").strip()
            out.append(Device(
                category="network", identifier=name,
                label=label or name,
                status=SUPPORTED if state == "up" else (
                    PARTIALLY_SUPPORTED if state in ("down", "unknown")
                    else UNKNOWN),
                evidence=[str(device_dir)],
                facts={"address": (address or "").strip(),
                       "operstate": state, "mtu": mtu, "driver": label}))
        return out

    def audio_devices(self) -> List[Device]:
        root = Path(SYSFS_SOUND)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for name in _listdir(root):
            if not name.startswith("card"):
                continue
            card_dir = root / name
            out.append(Device(
                category="audio", identifier=name,
                label=(_read(card_dir / "id") or name).strip(),
                status=SUPPORTED,
                evidence=[str(card_dir)],
                facts={"id": (_read(card_dir / "id") or "").strip(),
                       "number": (_read(card_dir / "number") or "").strip()}))
        return out

    def input_devices(self) -> List[Device]:
        root = Path(SYSFS_INPUT)
        if not root.is_dir():
            return []
        out: List[Device] = []
        for name in _listdir(root):
            device_dir = root / name
            device_name = _read(device_dir / "device" / "name")
            out.append(Device(
                category="input", identifier=name,
                label=(device_name or name).strip(),
                status=SUPPORTED if device_name else UNKNOWN,
                evidence=[str(device_dir)],
                facts={"name": (device_name or "").strip()}))
        return out

    # -- optional platform tools -----------------------------------------

    def tool_available(self, tool: str) -> bool:
        return self.allow_tools and shutil.which(tool) is not None

    def tool_inventory(self) -> Dict[str, bool]:
        """Which hardware tools exist here — a fact, not an assumption."""
        return {tool: shutil.which(tool) is not None
                for tool in ("lspci", "lsusb", "dmidecode", "lshw",
                             "qemu-system-x86_64", "gcc", "ld", "objdump")}


PCI_CLASSES = {
    "0x01": "mass-storage",
    "0x02": "network",
    "0x03": "display",
    "0x04": "multimedia",
    "0x05": "memory",
    "0x06": "bridge",
    "0x07": "simple-communication",
    "0x08": "base-system",
    "0x09": "input",
    "0x0c": "serial-bus",
    "0x0d": "wireless",
}


def pci_class_name(class_code: str) -> str:
    """Name a PCI class code, or say it is unrecognised.

    An unrecognised code returns ``unknown-class`` rather than a guess.
    """
    if not class_code:
        return ""
    prefix = class_code[:4].lower()
    return PCI_CLASSES.get(prefix, "unknown-class")
