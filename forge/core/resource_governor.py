"""Unified resource governor (Session 10).

One canonical budget surface for the whole process: CPU, RAM, disk,
network, concurrency, model memory, cost budget, and wall-clock time.
Everything that used to carry an ad-hoc limit (server worker pools,
runtime model loads, desktop execution) asks the governor instead of
inventing its own number.

Design rules

* **Restrict only.** The governor can deny, clamp, or budget an
  operation. It never creates capability, never grants permissions, and
  never bypasses the A33 policy gate — it sits *below* authorization.
* **Fail closed and honest.** A resource that cannot be measured is
  reported as ``None`` with ``measured=False`` — the governor never
  invents a reading. An explicit profile always wins over detection.
* **Bounded by construction.** Concurrency is a ``BoundedSemaphore``:
  a caller cannot ask for more slots than exist, and no code path
  spawns unbounded threads.

Profiles
--------

``default``
    A general-purpose machine. Sensible bounds derived from the
    measured hardware; local model loading allowed within the model
    memory budget.

``g560``
    The 2 GB thin-client device. It is **never an inference
    machine**: local model loading is denied outright (heavy work is
    offloaded to a Forge Server), concurrency is small, and there is
    no local model memory budget. Selecting it is explicit and can
    never be loosened by hardware detection.

Auto-detection (``detect_profile``) is advisory: a machine with
<= 2.5 GB of usable memory is classified as a G560-class device;
anything else (including unmeasurable) gets ``default``.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Tuple

#: A machine with no more usable RAM than this is a G560-class device.
G560_MEMORY_CEILING_MB = 2560

#: Network policies a profile may carry.
NETWORK_OFF = "off"                 # no network at all
NETWORK_SERVER_ONLY = "server-only"  # only toward a configured Forge Server
NETWORK_EXPLICIT = "explicit"       # operator explicitly enabled outbound


class ResourceLimitExceeded(Exception):
    """A governed budget is exhausted (or the op is profile-denied)."""

    code = "RESOURCE_LIMIT"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResourceBudget:
    """Hard bounds for one process under one profile.

    ``0`` means *no explicit bound* for that dimension (except
    ``model_memory_mb`` where ``0`` means *no local model memory at
    all* — a thin client loads nothing).
    """
    max_workers: int = 4
    max_task_wall_seconds: float = 600.0
    model_loading_allowed: bool = True
    model_memory_mb: int = 0
    max_cost_usd: float = 0.0
    scratch_disk_mb: int = 0
    network_policy: str = NETWORK_OFF


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    budget: ResourceBudget
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        b = self.budget
        return {
            "name": self.name,
            "rationale": self.rationale,
            "max_workers": b.max_workers,
            "max_task_wall_seconds": b.max_task_wall_seconds,
            "model_loading_allowed": b.model_loading_allowed,
            "model_memory_mb": b.model_memory_mb,
            "max_cost_usd": b.max_cost_usd,
            "scratch_disk_mb": b.scratch_disk_mb,
            "network_policy": b.network_policy,
        }


def _default_budget(cpu_count: Optional[int]) -> ResourceBudget:
    workers = 4
    if cpu_count and cpu_count > 0:
        workers = max(1, min(4, cpu_count))
    return ResourceBudget(
        max_workers=workers,
        max_task_wall_seconds=600.0,
        model_loading_allowed=True,
        model_memory_mb=0,
        max_cost_usd=0.0,
        scratch_disk_mb=0,
        network_policy=NETWORK_OFF,
    )


def default_profile(cpu_count: Optional[int] = None) -> ResourceProfile:
    return ResourceProfile(
        name="default",
        budget=_default_budget(cpu_count),
        rationale="General-purpose machine; bounds derived from "
                  "measured hardware, local model loading within "
                  "budget.")


def g560_profile() -> ResourceProfile:
    return ResourceProfile(
        name="g560",
        budget=ResourceBudget(
            max_workers=2,
            max_task_wall_seconds=300.0,
            model_loading_allowed=False,
            model_memory_mb=0,
            max_cost_usd=0.0,
            scratch_disk_mb=128,
            network_policy=NETWORK_SERVER_ONLY,
        ),
        rationale="2 GB thin client: never an inference machine. "
                  "Local model loading is denied; heavy work is "
                  "offloaded to a Forge Server. Concurrency is small "
                  "and bounded.",
    )


BUILTIN_PROFILES: Dict[str, Any] = {
    "default": lambda: default_profile(),
    "g560": lambda: g560_profile(),
}

ENV_PROFILE = "FORGE_RESOURCE_PROFILE"


def profile_from_name(name: str) -> ResourceProfile:
    """Resolve a profile by name; unknown names fail closed to g560
    (the most restrictive built-in) instead of guessing."""
    key = (name or "").strip().lower()
    if not key:
        return g560_profile()
    factory = BUILTIN_PROFILES.get(key)
    if factory is None:
        # Fail closed: an unrecognized profile is treated as the
        # restrictive device class, and the refusal is visible in the
        # snapshot (never a silent default loosening).
        profile = g560_profile()
        profile = ResourceProfile(
            name="unknown:%s" % key,
            budget=profile.budget,
            rationale=("Unknown profile %r; failing closed to the "
                       "restrictive g560 bounds." % key),
        )
        return profile
    return factory()


def measure_memory_mb() -> Tuple[Optional[int], Optional[int]]:
    """Return (total_mb, available_mb); ``None`` when unmeasurable.

    Stdlib only: ``/proc/meminfo`` on Linux, ``GlobalMemoryStatusEx``
    on Windows. Never raises.
    """
    if os.name == "posix" and os.path.exists("/proc/meminfo"):
        try:
            total_kb = None
            avail_kb = None
            with open("/proc/meminfo", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb is not None and avail_kb is not None:
                        break
            if total_kb is not None:
                return (total_kb // 1024,
                        avail_kb // 1024 if avail_kb is not None
                        else None)
        except (OSError, ValueError, IndexError):
            pass
        return (None, None)
    if os.name == "nt":
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = (
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                )
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return (int(stat.ullTotalPhys) // (1024 * 1024),
                    int(stat.ullAvailPhys) // (1024 * 1024))
        except Exception:
            return (None, None)
    return (None, None)


def system_resources() -> Dict[str, Any]:
    """Honest machine snapshot. Unmeasurable values are ``None``."""
    total_mb, avail_mb = measure_memory_mb()
    cpu = os.cpu_count()
    disk: Dict[str, Any] = {"total_mb": None, "free_mb": None}
    try:
        usage = shutil.disk_usage(os.getcwd())
        disk["total_mb"] = usage.total // (1024 * 1024)
        disk["free_mb"] = usage.free // (1024 * 1024)
    except OSError:
        pass
    return {
        "cpu_count": cpu,
        "memory_total_mb": total_mb,
        "memory_available_mb": avail_mb,
        "memory_measured": total_mb is not None,
        "disk": disk,
        "disk_measured": disk["total_mb"] is not None,
    }


def detect_profile(resources: Optional[Dict[str, Any]] = None
                   ) -> ResourceProfile:
    """Advisory device-class detection (explicit profiles always win).

    <= 2.5 GB usable memory  -> g560-class (restrictive).
    Unmeasurable memory      -> default (and flagged unmeasured in the
                                snapshot; detection is advisory, not a
                                capability grant).
    """
    resources = resources if resources is not None else system_resources()
    total_mb = resources.get("memory_total_mb")
    if total_mb is not None and int(total_mb) <= G560_MEMORY_CEILING_MB:
        return g560_profile()
    return default_profile(resources.get("cpu_count"))


def select_profile(explicit: str = "") -> ResourceProfile:
    """Resolve which profile applies: explicit name > env > detection."""
    name = (explicit or "").strip() or os.environ.get(ENV_PROFILE, "")
    if name:
        return profile_from_name(name)
    return detect_profile()


class ResourceGovernor:
    """The one canonical budget authority for a process.

    Construct with a resolved :class:`ResourceProfile` (or none for
    auto-selection). All counters are process-wide and thread-safe;
    the concurrency gate is a ``BoundedSemaphore`` so callers cannot
    request more slots than exist.
    """

    def __init__(self, profile: Optional[ResourceProfile] = None,
                 resources: Optional[Dict[str, Any]] = None) -> None:
        self._profile = profile or select_profile("")
        self._resources = resources if resources is not None \
            else system_resources()
        self._lock = threading.RLock()
        budget = self._profile.budget
        self._semaphore = threading.BoundedSemaphore(budget.max_workers)
        self._cost_usd = 0.0
        self._scratch_bytes = 0
        self._denied: list = []
        self._acquired_peak = 0
        self._acquired_now = 0

    # -- introspection ---------------------------------------------------

    @property
    def profile(self) -> ResourceProfile:
        return self._profile

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "profile": self._profile.to_dict(),
                "resources": dict(self._resources),
                "usage": {
                    "concurrency_now": self._acquired_now,
                    "concurrency_peak": self._acquired_peak,
                    "concurrency_capacity":
                        self._profile.budget.max_workers,
                    "cost_usd": round(self._cost_usd, 6),
                    "scratch_bytes": self._scratch_bytes,
                    "denials": list(self._denied[-20:]),
                },
            }

    # -- concurrency ------------------------------------------------------

    def clamp_workers(self, requested: int) -> int:
        """Clamp a requested worker count to the profile bound."""
        requested = max(1, int(requested or 1))
        return max(1, min(requested, self._profile.budget.max_workers))

    @contextmanager
    def concurrency(self) -> Iterator[None]:
        """One governed worker slot. Bounded: cannot exceed capacity."""
        self._semaphore.acquire()
        try:
            with self._lock:
                self._acquired_now += 1
                self._acquired_peak = max(self._acquired_peak,
                                          self._acquired_now)
            yield
        finally:
            with self._lock:
                self._acquired_now -= 1
            self._semaphore.release()

    # -- model memory ------------------------------------------------------

    def check_model_load(self, size_bytes: int) -> Tuple[bool, str]:
        """May a model of this size be loaded locally?

        Honest refusals: profile-denied (e.g. g560) or over the model
        memory budget. A zero budget with loading allowed means no
        explicit bound (default profile) — but a zero budget with
        loading denied means *nothing may load* (g560).
        """
        budget = self._profile.budget
        if not budget.model_loading_allowed:
            return (False,
                    "local model loading is denied by the %r profile "
                    "(%s); heavy work must be offloaded to a server"
                    % (self._profile.name, budget.network_policy))
        if budget.model_memory_mb > 0:
            cap = int(budget.model_memory_mb) * 1024 * 1024
            if int(size_bytes or 0) > cap:
                return (False,
                        "model of %d bytes exceeds the %d MB model "
                        "memory budget" % (int(size_bytes or 0),
                                           budget.model_memory_mb))
        return (True, "ok")

    # -- cost budget --------------------------------------------------------

    def spend_cost(self, usd: float) -> None:
        """Record spend; raises when the cost budget is exhausted."""
        usd = float(usd or 0.0)
        if usd < 0:
            raise ResourceLimitExceeded("BAD_COST", "cost must be >= 0")
        cap = self._profile.budget.max_cost_usd
        with self._lock:
            if cap > 0 and self._cost_usd + usd > cap:
                self._deny("COST_BUDGET",
                           "spending $%.4f would exceed the $%.2f budget"
                           % (usd, cap))
                raise ResourceLimitExceeded(
                    "COST_BUDGET",
                    "cost budget exhausted (spent $%.4f of $%.2f)"
                    % (self._cost_usd, cap))
            self._cost_usd += usd

    # -- disk scratch --------------------------------------------------------

    def scratch_reserve(self, nbytes: int) -> None:
        """Reserve scratch disk; bounded by the profile budget."""
        nbytes = int(nbytes or 0)
        if nbytes < 0:
            raise ResourceLimitExceeded("BAD_SCRATCH",
                                        "bytes must be >= 0")
        cap_mb = self._profile.budget.scratch_disk_mb
        with self._lock:
            if cap_mb > 0:
                cap = int(cap_mb) * 1024 * 1024
                if self._scratch_bytes + nbytes > cap:
                    self._deny(
                        "SCRATCH_BUDGET",
                        "reserving %d bytes would exceed the %d MB "
                        "scratch budget" % (nbytes, cap_mb))
                    raise ResourceLimitExceeded(
                        "SCRATCH_BUDGET",
                        "scratch budget exhausted (%d of %d bytes held)"
                        % (self._scratch_bytes, cap))
            self._scratch_bytes += nbytes

    def scratch_release(self, nbytes: int) -> None:
        with self._lock:
            self._scratch_bytes = max(
                0, self._scratch_bytes - int(nbytes or 0))

    # -- time ---------------------------------------------------------------

    @contextmanager
    def deadline(self, seconds: Optional[float] = None) -> Iterator[float]:
        """Wall-clock bound: yields the deadline; raises on expiry.

        The bound is the profile's ``max_task_wall_seconds`` unless a
        smaller explicit bound is given.
        """
        bound = self._profile.budget.max_task_wall_seconds
        if seconds is not None:
            bound = min(bound, float(seconds))
        limit = max(0.0, float(bound))
        end = time.monotonic() + limit
        yield end
        if time.monotonic() > end:
            self._deny("WALL_TIME", "wall-clock bound of %.1fs exceeded"
                       % limit)
            raise ResourceLimitExceeded(
                "WALL_TIME", "wall-clock bound of %.1fs exceeded" % limit)

    # -- internals -----------------------------------------------------------

    def _deny(self, code: str, message: str) -> None:
        with self._lock:
            self._denied.append({"code": code, "message": message,
                                 "at": time.time()})
            if len(self._denied) > 100:
                self._denied = self._denied[-100:]
