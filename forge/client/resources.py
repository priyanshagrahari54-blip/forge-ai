"""Bounded local resource probe for the lightweight client (A81).

Answers "can this machine execute a light task right now?" without any
third-party dependency. Everything is injectable for deterministic
tests. The real probe reads, in order of preference:

1. ``/proc/meminfo`` (Linux — MemTotal / MemAvailable);
2. ``ctypes`` ``GlobalMemoryStatusEx`` (Windows — real numbers, not a
   guess);
3. ``resource.getrusage`` + ``sysconf`` (macOS/BSD — total via
   ``HW_MEMSIZE``-style sysconf, free estimated from resident memory).

If every source fails, the snapshot reports ``source="unavailable"``
with **zero** free RAM: the router then refuses LOCAL execution for
capacity reasons (fail closed) instead of pretending the machine is
idle or overloaded.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ResourceSnapshot:
    cpus: int
    total_ram_mb: int
    free_ram_mb: int
    #: Honest label of where the numbers came from (tests inject "test").
    source: str = "probe"

    def as_dict(self) -> dict:
        return {"cpus": self.cpus, "total_ram_mb": self.total_ram_mb,
                "free_ram_mb": self.free_ram_mb, "source": self.source}

    def adequate(self, *, min_free_ram_mb: int, min_cpus: int = 1) -> bool:
        return (self.cpus >= min_cpus
                and self.free_ram_mb >= max(0, min_free_ram_mb))


def probe(meminfo_path: str = "/proc/meminfo") -> ResourceSnapshot:
    """Real probe: procfs (Linux) -> GlobalMemoryStatusEx (Windows) ->
    sysconf (macOS/BSD) -> honest unavailable (zero free, fail closed)."""
    cpus = os.cpu_count() or 1
    total_mb, free_mb, source = _memory(meminfo_path)
    return ResourceSnapshot(cpus=cpus, total_ram_mb=total_mb,
                            free_ram_mb=free_mb, source=source)


def _memory(meminfo_path: str) -> tuple[int, int, str]:
    if os.path.exists(meminfo_path):
        total_kb, avail_kb = _meminfo(meminfo_path)
        if total_kb > 0:
            return total_kb // 1024, avail_kb // 1024, "proc"
    if sys.platform == "win32":
        values = _windows_memory()
        if values is not None:
            return values[0] // (1024 * 1024), values[1] // (1024 * 1024), \
                "win32"
    if sys.platform == "darwin":
        values = _darwin_memory()
        if values is not None:
            return values
    # Fallback used only when nothing could be measured: report zero
    # free RAM so LOCAL is refused (fail closed), never guessed.
    return 0, 0, "unavailable"


def _meminfo(path: str) -> tuple[int, int]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        number = rest.strip().split(" ", 1)[0]
        if key and number.isdigit():
            values[key.strip()] = int(number)
    total_kb = values.get("MemTotal", 0)
    available_kb = values.get("MemAvailable", values.get("MemFree", 0))
    return total_kb, available_kb


def _windows_memory() -> Optional[tuple[int, int]]:
    """GlobalMemoryStatusEx via ctypes: (total_phys, avail_phys) bytes."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except Exception:  # noqa: BLE001 - any failure -> next source
        return None
    return None


def _darwin_memory() -> Optional[tuple[int, int]]:
    """macOS: total via sysconf HW_MEMSIZE; free estimated conservatively
    from the resident size of this process. Deliberately pessimistic: an
    underestimate can only refuse LOCAL, never allow a heavy run on a
    loaded machine. Returns (total_mb, free_mb) or None."""
    try:
        import resource

        total = os.sysconf("HW_MEMSIZE") if hasattr(os, "sysconf") else 0
        if total <= 0:
            return None
        resident_bytes = 0
        try:
            # ru_maxrss: KiB on macOS, bytes on Linux.
            resident = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            resident_bytes = resident * 1024 if sys.platform == "darwin" \
                else resident
        except (ValueError, OSError):
            resident_bytes = 0
        # Conservative free estimate: total minus a fixed OS reserve
        # (2 GiB) minus this process's resident footprint.
        reserve = 2 * 1024 * 1024 * 1024
        free = max(0, total - reserve - resident_bytes)
        return total // (1024 * 1024), free // (1024 * 1024)
    except Exception:  # noqa: BLE001 - any failure -> unavailable
        return None


def current(min_free_ram_mb: int, snapshot: Optional[ResourceSnapshot] = None,
            meminfo_path: str = "/proc/meminfo") -> ResourceSnapshot:
    """Snapshot to use for a decision (explicit snapshot wins)."""
    return snapshot or probe(meminfo_path)
