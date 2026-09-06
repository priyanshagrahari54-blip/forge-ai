"""Health tracking for models in the Model Fabric.

Health is derived entirely from recorded outcomes (router feedback). A model
degrades after consecutive failures and is marked unhealthy past a hard bound,
so routing can fail over deterministically without any operator intervention.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HealthStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ModelHealth:
    """Mutable health state for a single model."""

    status: str = HealthStatus.UNKNOWN.value
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_error: str = ""
    last_check: float = 0.0
    #: Consecutive failures before the model is considered degraded.
    degrade_after: int = 2
    #: Consecutive failures before the model is considered unhealthy.
    unhealthy_after: int = 5
    #: Consecutive successes before an unhealthy model recovers.
    recover_after: int = 2

    def record_success(self) -> None:
        self.total_successes += 1
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        self.last_error = ""
        self.last_check = time.time()
        self._recompute()

    def record_failure(self, error: str = "") -> None:
        self.total_failures += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.last_error = error or self.last_error
        self.last_check = time.time()
        self._recompute()

    def _recompute(self) -> None:
        if self.consecutive_failures >= self.unhealthy_after:
            self.status = HealthStatus.UNHEALTHY.value
        elif self.consecutive_failures >= self.degrade_after:
            self.status = HealthStatus.DEGRADED.value
        elif self.consecutive_successes >= self.recover_after:
            self.status = HealthStatus.HEALTHY.value
        elif self.consecutive_failures == 0 and self.consecutive_successes == 0:
            self.status = HealthStatus.UNKNOWN.value

    @property
    def usable(self) -> bool:
        """Healthy, degraded, or unknown models are routable; unhealthy is not."""
        return self.status != HealthStatus.UNHEALTHY.value

    @property
    def last_resort(self) -> bool:
        """An unhealthy model is only ever a deterministic last resort."""
        return self.status == HealthStatus.UNHEALTHY.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
            "last_check": self.last_check,
        }
