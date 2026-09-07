"""Router feedback types for the Model Fabric.

Feedback is the structured record of a single routed model call. It carries no
prompt/response content, only outcomes and measurements, so it is safe to
persist and safe to use for future routing decisions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouterFeedback:
    model: str
    provider: str = ""
    capability: str = ""
    success: bool = True
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    complexity: float | None = None
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "capability": self.capability,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "complexity": self.complexity,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouterFeedback":
        return cls(
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            capability=data.get("capability", ""),
            success=bool(data.get("success", True)),
            latency_ms=float(data["latency_ms"]) if data.get("latency_ms") is not None else None,
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            complexity=data.get("complexity"),
            error=data.get("error", ""),
            timestamp=float(data.get("timestamp", time.time())),
        )
