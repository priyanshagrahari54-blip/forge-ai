"""Transactional resource reservations (F).

A small orchestration primitive over the canonical ResourceGovernor.  A
reservation owns every resource it acquires and releases those resources
on every exit path.  This keeps workers from leaking concurrency slots or
scratch reservations when execution fails, is cancelled, or raises.

The governor remains the authority: this module does not invent limits or
bypass authorization.  Cost is settled explicitly after execution because
provider spend is an outcome, not a reservable local resource.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from forge.core.resource_governor import ResourceGovernor, ResourceLimitExceeded


@dataclass
class ResourceReservation:
    """One owned reservation with a strict reserve/execute/release lifecycle."""

    governor: ResourceGovernor
    scratch_bytes: int = 0
    cost_usd: float = 0.0
    _scratch_held: bool = False
    _entered: bool = False
    _settled: bool = False

    def reserve(self) -> "ResourceReservation":
        """Reserve all local resources before execution starts.

        Scratch is reserved before the worker slot is acquired.  If slot
        acquisition is interrupted, the scratch reservation is released.
        A failed reservation never leaves partial ownership behind.
        """
        if self._entered or self._scratch_held:
            raise RuntimeError("resource reservation already active")
        if self.scratch_bytes < 0 or self.cost_usd < 0:
            raise ResourceLimitExceeded(
                "BAD_RESERVATION", "reservation values must be >= 0")
        self.governor.scratch_reserve(self.scratch_bytes)
        self._scratch_held = True
        try:
            self._concurrency = self.governor.concurrency()
            self._concurrency.__enter__()
            self._entered = True
        except BaseException:
            self.governor.scratch_release(self.scratch_bytes)
            self._scratch_held = False
            raise
        return self

    def settle_cost(self, actual_cost_usd: Optional[float] = None) -> None:
        """Record provider spend exactly once after successful execution."""
        if not self._entered:
            raise RuntimeError("cannot settle an inactive reservation")
        if self._settled:
            raise RuntimeError("reservation cost already settled")
        amount = self.cost_usd if actual_cost_usd is None else float(actual_cost_usd)
        self.governor.spend_cost(amount)
        self._settled = True

    def release(self) -> None:
        """Release every resource owned by this reservation."""
        if self._entered:
            self._concurrency.__exit__(None, None, None)
            self._entered = False
        if self._scratch_held:
            self.governor.scratch_release(self.scratch_bytes)
            self._scratch_held = False

    def __enter__(self) -> "ResourceReservation":
        return self.reserve()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def reserve_resources(governor: ResourceGovernor, *, scratch_bytes: int = 0,
                      cost_usd: float = 0.0) -> ResourceReservation:
    """Create an unstarted reservation; use as a context manager."""
    return ResourceReservation(governor, int(scratch_bytes), float(cost_usd))
