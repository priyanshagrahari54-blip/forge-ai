"""Structured request/response types for the Model Fabric."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class ModelRequest:
    """A structured, capability-aware request to the Model Fabric.

    Routing consumes the capability and hard bounds; the prompt/context/task
    are forwarded verbatim to the selected provider. Raw content is never
    persisted to telemetry.
    """

    prompt: str = ""
    #: Primary capability to route on (canonical string from
    #: ``forge.models.capabilities``).
    capability: str = "coding"
    #: Additional capabilities the model must also support. If empty, the
    #: primary capability is used.
    required_capabilities: tuple[str, ...] = ()
    context: str = ""
    task: str = ""
    min_context_window: int = 0
    max_output_tokens: int | None = None
    #: 1.0 = trivial, higher = more complex. Used for complexity-aware routing.
    complexity: float = 1.0
    #: Per-request overrides for the routing policy preferences. ``None`` means
    #: "defer to the policy".
    prefer_free: bool | None = None
    prefer_local: bool | None = None
    max_cost_per_token: float | None = None
    max_latency_ms: float | None = None
    temperature: float | None = None
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        capability: str = "coding",
        *,
        context: str = "",
        task: str = "",
        **kwargs: Any,
    ) -> "ModelRequest":
        return cls(prompt=prompt, capability=capability, context=context, task=task, **kwargs)

    def effective_capabilities(self) -> tuple[str, ...]:
        """Capabilities a routed model must support."""
        if self.required_capabilities:
            return tuple(self.required_capabilities)
        if self.capability:
            return (self.capability,)
        return ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_chars": len(self.prompt),
            "capability": self.capability,
            "required_capabilities": list(self.required_capabilities),
            "context_chars": len(self.context),
            "task": self.task,
            "min_context_window": self.min_context_window,
            "max_output_tokens": self.max_output_tokens,
            "complexity": self.complexity,
            "trace_id": self.trace_id,
        }


@dataclass
class ModelResponse:
    """A structured, provider-agnostic response from the Model Fabric.

    ``success=False`` responses carry an ``error`` and an empty ``text`` so
    callers can branch deterministically without inspecting provider types.
    """

    text: str = ""
    model: str = ""
    provider: str = ""
    success: bool = True
    error: str = ""
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, error: str, *, provider: str = "", request_id: str = "", **kwargs: Any) -> "ModelResponse":
        return cls(success=False, error=error, provider=provider, request_id=request_id, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "model": self.model,
            "provider": self.provider,
            "error": self.error,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "text_chars": len(self.text),
        }
