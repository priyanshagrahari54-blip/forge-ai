"""Admission policy that layers autonomous budgets over the persistent queue.
The persistent task queue remains the source of truth; this module only gates
how many queued tasks may execute concurrently and how retries are bounded.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutonomousAdmission:
    max_active_tasks: int = 4
    max_attempts_per_task: int = 8
    allow_provider_rotation: bool = True
    allow_capability_discovery: bool = True

    def can_start(self, active_tasks: int) -> bool:
        return active_tasks < max(1, self.max_active_tasks)

    def can_retry(self, attempts: int) -> bool:
        return attempts < max(1, self.max_attempts_per_task)

    def policy(self) -> dict[str, object]:
        return {
            "max_active_tasks": max(1, self.max_active_tasks),
            "max_attempts_per_task": max(1, self.max_attempts_per_task),
            "allow_provider_rotation": self.allow_provider_rotation,
            "allow_capability_discovery": self.allow_capability_discovery,
        }
