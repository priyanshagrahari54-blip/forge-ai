from dataclasses import dataclass


@dataclass
class ModelInfo:
    name: str
    capability: str
    available: bool = False
    context_size: int = 4096
    historical_success_rate: float = 1.0
    latency: float = 0.0
    failure_rate: float = 0.0
    task_complexity: float = 1.0
    cost_per_token: float = 0.0


class ModelRouter:
    def __init__(self) -> None:
        self.models: list[ModelInfo] = []

    def register(self, model: ModelInfo) -> None:
        self.models.append(model)

    def select(
        self,
        capability: str,
        min_context_size: int = 0,
        max_cost: float | None = None,
        max_latency: float | None = None,
        task_complexity: float | None = None,
    ) -> ModelInfo | None:
        candidates = [
            model
            for model in self.models
            if model.capability == capability and model.available
        ]

        if min_context_size > 0:
            candidates = [m for m in candidates if m.context_size >= min_context_size]

        if max_cost is not None:
            candidates = [m for m in candidates if m.cost_per_token <= max_cost]

        if max_latency is not None:
            candidates = [m for m in candidates if m.latency <= max_latency]

        if task_complexity is not None:
            candidates = [m for m in candidates if m.task_complexity >= task_complexity]

        if not candidates:
            return None

        candidates.sort(
            key=lambda m: (-m.historical_success_rate, m.failure_rate, m.latency)
        )

        return candidates[0]
