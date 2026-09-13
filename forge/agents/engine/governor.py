"""Resource limits (A83): declared bounds, enforced while running.

The spec's :class:`~forge.agents.engine.spec.ResourceLimits` are not
advisory. A run acquires a :class:`RunBudget` before it does anything and
charges every model call, file write, and output byte against it; the
governor refuses the run before it starts when the hourly or concurrency
limit is already reached, so a refusal has no side effects at all.

Counters live in memory for the lifetime of the process (a restart resets
the hourly window), while the durable record of what actually happened is
the package's ``history.json``.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from forge.agents.engine.errors import AgentLimitError
from forge.agents.engine.spec import ResourceLimits

WINDOW_SECONDS = 3600.0


@dataclass
class RunBudget:
    """Per-run counters charged as the run consumes resources."""

    limits: ResourceLimits
    started_at: float
    model_calls: int = 0
    file_writes: int = 0
    output_bytes: int = 0
    tool_calls: int = 0

    def charge_model(self) -> None:
        self.model_calls += 1
        if self.model_calls > self.limits.max_model_calls:
            raise AgentLimitError(
                "Model call limit reached (%d)"
                % self.limits.max_model_calls)

    def charge_write(self, count: int = 1) -> None:
        self.file_writes += count
        if self.file_writes > self.limits.max_file_writes:
            raise AgentLimitError(
                "File write limit reached (%d)" % self.limits.max_file_writes)

    def charge_output(self, count: int) -> None:
        self.output_bytes += max(0, int(count))
        if self.output_bytes > self.limits.max_output_bytes:
            raise AgentLimitError(
                "Output limit reached (%d bytes)"
                % self.limits.max_output_bytes)

    def charge_tool(self) -> None:
        self.tool_calls += 1

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def expired(self) -> bool:
        return self.elapsed() > self.limits.max_wall_seconds

    def check_wall(self) -> None:
        if self.expired():
            raise AgentLimitError(
                "Wall clock limit reached (%.1fs)"
                % self.limits.max_wall_seconds)

    def to_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "file_writes": self.file_writes,
            "output_bytes": self.output_bytes,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed(), 3),
            "limits": self.limits.to_dict(),
        }


class AgentGovernor:
    """Hourly and concurrency accounting per agent name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._starts: dict = {}
        self._active: dict = {}

    def check(self, agent: str, limits: ResourceLimits) -> tuple:
        """``(allowed, reason)`` for starting a run right now."""
        now = time.time()
        with self._lock:
            starts = self._starts.setdefault(agent, deque())
            while starts and now - starts[0] > WINDOW_SECONDS:
                starts.popleft()
            active = self._active.get(agent, 0)
        if len(starts) >= limits.max_runs_per_hour:
            return False, ("hourly run limit reached (%d)"
                           % limits.max_runs_per_hour)
        if active >= limits.max_concurrent:
            return False, ("concurrency limit reached (%d)"
                           % limits.max_concurrent)
        return True, ""

    def begin(self, agent: str, limits: ResourceLimits) -> RunBudget:
        """Reserve a run slot, or raise :class:`AgentLimitError`."""
        allowed, reason = self.check(agent, limits)
        if not allowed:
            raise AgentLimitError("Agent %r: %s" % (agent, reason))
        with self._lock:
            self._starts.setdefault(agent, deque()).append(time.time())
            self._active[agent] = self._active.get(agent, 0) + 1
        return RunBudget(limits=limits, started_at=time.time())

    def end(self, agent: str) -> None:
        with self._lock:
            self._active[agent] = max(0, self._active.get(agent, 0) - 1)

    def usage(self, agent: str, limits: ResourceLimits) -> dict:
        now = time.time()
        with self._lock:
            starts = [at for at in self._starts.get(agent, ())
                      if now - at <= WINDOW_SECONDS]
            active = self._active.get(agent, 0)
        return {"agent": agent, "runs_last_hour": len(starts),
                "active_runs": active,
                "max_runs_per_hour": limits.max_runs_per_hour,
                "max_concurrent": limits.max_concurrent,
                "window_seconds": WINDOW_SECONDS}
