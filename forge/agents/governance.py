"""Agent governance (A57): enforced per-agent runtime quotas.

Limits are enforced where runs are dispatched — the control plane
refuses before any work starts, so a quota violation has no side
effects. Limits can only restrict (they can never grant anything),
and every refusal is audited.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

DEFAULT_RUNS_PER_HOUR = 60
DEFAULT_MAX_CONCURRENT = 2
WINDOW = 3600.0


@dataclass
class AgentLimits:
    max_runs_per_hour: int = DEFAULT_RUNS_PER_HOUR
    max_concurrent: int = DEFAULT_MAX_CONCURRENT

    def to_dict(self) -> dict[str, Any]:
        return {"max_runs_per_hour": self.max_runs_per_hour,
                "max_concurrent": self.max_concurrent}


class AgentGovernor:
    """Bounded counters enforcing per-agent quotas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limits: dict[str, AgentLimits] = {}
        self._starts: dict[str, deque[float]] = {}
        self._active: dict[str, int] = {}

    def set_limits(self, agent: str, *, max_runs_per_hour: int = 60,
                   max_concurrent: int = 2) -> dict[str, Any]:
        if max_runs_per_hour < 1 or max_runs_per_hour > 1000:
            raise ValueError("max_runs_per_hour must be 1-1000")
        if max_concurrent < 1 or max_concurrent > 20:
            raise ValueError("max_concurrent must be 1-20")
        with self._lock:
            self._limits[agent] = AgentLimits(
                max_runs_per_hour=max_runs_per_hour,
                max_concurrent=max_concurrent)
        return self.limits(agent)

    def limits(self, agent: str) -> dict[str, Any]:
        with self._lock:
            limits = self._limits.get(agent, AgentLimits())
            starts = list(self._starts.get(agent, ()))
            active = self._active.get(agent, 0)
        return {"agent": agent, **limits.to_dict(),
                "runs_last_hour": len(starts),
                "active_runs": active,
                "window_seconds": WINDOW}

    def check(self, agent: str) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            limits = self._limits.get(agent, AgentLimits())
            starts = self._starts.setdefault(agent, deque())
            while starts and now - starts[0] > WINDOW:
                starts.popleft()
            active = self._active.get(agent, 0)
            if len(starts) >= limits.max_runs_per_hour:
                return False, (
                    f"Agent {agent!r} hit its hourly run limit "
                    f"({limits.max_runs_per_hour})")
            if active >= limits.max_concurrent:
                return False, (
                    f"Agent {agent!r} hit its concurrency limit "
                    f"({limits.max_concurrent})")
        return True, ""

    def begin(self, agent: str) -> None:
        with self._lock:
            self._starts.setdefault(agent, deque()).append(time.time())
            self._active[agent] = self._active.get(agent, 0) + 1

    def end(self, agent: str) -> None:
        with self._lock:
            self._active[agent] = max(0, self._active.get(agent, 0) - 1)
