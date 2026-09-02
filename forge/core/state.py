from dataclasses import dataclass, field
from enum import Enum


class ForgeStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass
class ForgeState:
    project_name: str
    status: ForgeStatus = ForgeStatus.IDLE
    current_task: str | None = None
    iteration: int = 0
    errors: list[str] = field(default_factory=list)

    def start_task(self, task: str) -> None:
        self.current_task = task
        self.status = ForgeStatus.RUNNING
        self.iteration += 1

    def complete_task(self) -> None:
        self.status = ForgeStatus.COMPLETED

    def fail(self, error: str) -> None:
        self.status = ForgeStatus.FAILED
        self.errors.append(error)
