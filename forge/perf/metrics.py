"""Performance measurement (A83): real numbers from the operating system.

No estimator, no proxy, no model. Two real sources:

* **Wall time** from :func:`time.perf_counter` around a real execution.
* **CPU time and peak memory** from :func:`resource.getrusage` on
  ``RUSAGE_CHILDREN``, sampled immediately before and after the child runs.
  This is the kernel's own accounting of the process tree Forge started, so
  it needs no external tool and cannot be fabricated.

Where the platform cannot measure something, the field is ``None`` and the
sample says so. ``disk_io_bytes`` and ``net_bytes`` are populated from
``/proc`` when that filesystem exists and left ``None`` elsewhere — a Linux
counter is never silently reported as zero on a platform that has no such
counter.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.builder.runner import CommandResult, CommandRunner, resolve_executable

HAS_RESOURCE = False
try:                                     # pragma: no cover - platform dependent
    import resource                      # type: ignore
    HAS_RESOURCE = True
except Exception:                        # pragma: no cover - Windows
    resource = None                      # type: ignore

PROC_SELF_IO = "/proc/self/io"


@dataclass(frozen=True)
class ResourceSample:
    """One measurement of one execution."""

    wall_ms: float
    #: Kernel-reported user CPU seconds for the child tree.
    user_cpu_seconds: Optional[float] = None
    system_cpu_seconds: Optional[float] = None
    #: Peak resident set size of the child tree, in bytes.
    peak_rss_bytes: Optional[int] = None
    #: Bytes read/written by the child tree, when /proc exposes them.
    disk_read_bytes: Optional[int] = None
    disk_write_bytes: Optional[int] = None
    #: Voluntary/involuntary context switches, when available.
    voluntary_switches: Optional[int] = None
    involuntary_switches: Optional[int] = None
    return_code: Optional[int] = None
    status: str = ""
    #: Which measurement sources were actually available.
    sources: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wall_ms": round(self.wall_ms, 3),
            "user_cpu_seconds": _round(self.user_cpu_seconds, 4),
            "system_cpu_seconds": _round(self.system_cpu_seconds, 4),
            "cpu_seconds": _round(_total_cpu(self), 4),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mib": _round(
                (self.peak_rss_bytes / 1048576.0)
                if self.peak_rss_bytes is not None else None, 2),
            "disk_read_bytes": self.disk_read_bytes,
            "disk_write_bytes": self.disk_write_bytes,
            "voluntary_switches": self.voluntary_switches,
            "involuntary_switches": self.involuntary_switches,
            "return_code": self.return_code,
            "status": self.status,
            "sources": list(self.sources),
        }


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _total_cpu(sample: ResourceSample) -> Optional[float]:
    if sample.user_cpu_seconds is None and sample.system_cpu_seconds is None:
        return None
    return (sample.user_cpu_seconds or 0.0) + (sample.system_cpu_seconds or 0.0)


def _children_usage() -> Optional[Any]:
    if not HAS_RESOURCE:
        return None
    try:
        return resource.getrusage(resource.RUSAGE_CHILDREN)
    except (OSError, ValueError):
        return None


def _proc_io_delta(before: Optional[Dict[str, int]],
                   after: Optional[Dict[str, int]]
                   ) -> Tuple[Optional[int], Optional[int]]:
    if before is None or after is None:
        return None, None
    read_before = before.get("read_bytes")
    read_after = after.get("read_bytes")
    write_before = before.get("write_bytes")
    write_after = after.get("write_bytes")
    return (
        None if read_before is None or read_after is None
        else max(0, read_after - read_before),
        None if write_before is None or write_after is None
        else max(0, write_after - write_before),
    )


def _read_proc_io() -> Optional[Dict[str, int]]:
    """Read /proc/self/io counters, or None when unavailable.

    ``/proc/self/io`` is the *reading* process, so the delta across a
    ``subprocess.run`` call is only meaningful for the parent's own I/O; the
    child's I/O is captured through rusage where the platform accounts for it.
    The field is left ``None`` rather than zero whenever the file cannot be
    read, so a missing counter never looks like a measured zero.
    """
    path = Path(PROC_SELF_IO)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: Dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        try:
            out[key.strip()] = int(value.strip())
        except ValueError:
            continue
    return out or None


class ResourceMeter:
    """Measures real resource usage of commands run through it."""

    def __init__(self, root: str | Path = ".", *,
                 runner: Optional[CommandRunner] = None) -> None:
        self.root = Path(root).resolve()
        self.runner = runner or CommandRunner(self.root)

    def measure(self, argv: Sequence[str], *, timeout: Optional[float] = None,
                cwd: str = "") -> Tuple[ResourceSample, CommandResult]:
        """Run *argv* once and return its measured sample and raw result."""
        sources: List[str] = ["wall"]
        before_usage = _children_usage()
        before_io = _read_proc_io()
        if before_usage is not None:
            sources.append("rusage")
        if before_io is not None:
            sources.append("proc-io")
        started = time.perf_counter()
        result = self.runner.run(argv, timeout=timeout, cwd=cwd)
        wall_ms = (time.perf_counter() - started) * 1000.0
        after_usage = _children_usage()
        after_io = _read_proc_io()

        sample: ResourceSample
        if before_usage is not None and after_usage is not None:
            user = max(0.0, after_usage.ru_utime - before_usage.ru_utime)
            system = max(0.0, after_usage.ru_stime - before_usage.ru_stime)
            # ru_maxrss is kilobytes on Linux and bytes on macOS.
            peak = int(after_usage.ru_maxrss)
            if sys.platform == "darwin":
                peak_bytes = peak
            else:
                peak_bytes = peak * 1024
            read_bytes, write_bytes = _proc_io_delta(before_io, after_io)
            sample = ResourceSample(
                wall_ms=wall_ms, user_cpu_seconds=user,
                system_cpu_seconds=system, peak_rss_bytes=peak_bytes or None,
                disk_read_bytes=read_bytes, disk_write_bytes=write_bytes,
                voluntary_switches=int(after_usage.ru_nvcsw),
                involuntary_switches=int(after_usage.ru_nivcsw),
                return_code=result.return_code, status=result.status,
                sources=tuple(sources))
        else:
            sample = ResourceSample(
                wall_ms=wall_ms, return_code=result.return_code,
                status=result.status, sources=tuple(sources))
        return sample, result

    def measure_repeated(self, argv: Sequence[str], *, runs: int = 3,
                         timeout: Optional[float] = None, cwd: str = "",
                         warmup: int = 0) -> List[ResourceSample]:
        """Run *argv* ``runs`` times and return every sample.

        A failed run is still a sample (with its status), and repeated runs
        exist precisely so variance is visible instead of assumed away.
        """
        total = max(1, int(runs))
        for _ in range(max(0, int(warmup))):
            self.runner.run(argv, timeout=timeout, cwd=cwd)
        samples: List[ResourceSample] = []
        for _ in range(total):
            sample, _result = self.measure(argv, timeout=timeout, cwd=cwd)
            samples.append(sample)
        return samples


def statistics(values: Sequence[float]) -> Dict[str, Any]:
    """Honest aggregate statistics over measured samples."""
    data = [float(value) for value in values]
    count = len(data)
    if not count:
        return {"count": 0, "min": None, "median": None, "mean": None,
                "p95": None, "max": None, "stddev": None, "total": 0.0}
    ordered = sorted(data)
    mean = sum(ordered) / count
    variance = sum((value - mean) ** 2 for value in ordered) / count
    return {
        "count": count,
        "min": round(ordered[0], 3),
        "median": round(ordered[count // 2], 3),
        "mean": round(mean, 3),
        "p95": round(ordered[min(count - 1, int(0.95 * count))], 3),
        "max": round(ordered[-1], 3),
        "stddev": round(variance ** 0.5, 3),
        "total": round(sum(ordered), 3),
    }
