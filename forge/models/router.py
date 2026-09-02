from dataclasses import dataclass


@dataclass
class ModelInfo:
    name: str
    capability: str
    available: bool = False


class ModelRouter:
    def __init__(self) -> None:
        self.models: list[ModelInfo] = []

    def register(self, model: ModelInfo) -> None:
        self.models.append(model)

    def select(self, capability: str) -> ModelInfo | None:
        candidates = [
            model
            for model in self.models
            if model.capability == capability and model.available
        ]

        return candidates[0] if candidates else None
