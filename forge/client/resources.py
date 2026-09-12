"""Bounded local resource probe for the lightweight client (A81).

Answers "can this machine execute a light task right now?" without any
third-party dependency. Everything is injectable for deterministic
tests; the real probe reads ``os.cpu_count()`` and ``/proc/meminfo``
(Linux) with conservative fallbacks elsewhere.
"""
from __future__ import annotations

import os
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
    """Real probe (Linux: /proc/meminfo; elsewhere: conservative guess)."""
    cpus = os.cpu_count() or 1
    total_mb, free_mb = _meminfo(meminfo_path)
    if total_mb <= 0:
        # Non-Linux fallback: assume a small, honest default rather than
        # pretending; free is unknown -> report total as free is wrong,
        # so report 0 free (fail closed for local execution).
        total_mb, free_mb = 2048, 0
        return ResourceSnapshot(cpus=cpus, total_ram_mb=total_mb,
                                free_ram_mb=free_mb, source="fallback")
    return ResourceSnapshot(cpus=cpus, total_ram_mb=total_mb,
                            free_ram_mb=free_mb, source="proc")


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
    return total_kb // 1024, available_kb // 1024


def current(min_free_ram_mb: int, snapshot: Optional[ResourceSnapshot] = None,
            meminfo_path: str = "/proc/meminfo") -> ResourceSnapshot:
    """Snapshot to use for a decision (explicit snapshot wins)."""
    return snapshot or probe(meminfo_path)
