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
    # -- Session 11: inference-fabric addressing and governance -----------
    #: Explicit model selection (``"<backend>:<name>"`` or a bare name). The
    #: routing engine still refuses it when it is unsafe or unavailable.
    model: str = ""
    #: Explicit backend selection. Never "whichever backend answers".
    backend: str = ""
    #: Execution identity: a stale attempt may not publish a result.
    task_id: str = ""
    attempt_id: str = ""
    generation_id: str = ""
    #: Bounded wall-clock budget in seconds (clamped by the device profile).
    timeout: float | None = None
    #: Declared data classification (``public``/``internal``/``confidential``/
    #: ``secret``). Detection can still raise the level; it never lowers it.
    classification: str = ""
    #: Network policy override (``""`` = the device profile decides).
    network_policy: str = ""
    #: Explicit cost ceiling for this request, in USD.
    cost_budget_usd: float | None = None
    #: Device/hardware profile hint (``g560`` denies local model loading).
    hardware_profile: str = ""
    #: Require a *verified* model. Turning this off is explicit and recorded.
    require_verified: bool = True
    #: Allow the deterministic non-neural rung when no model can serve.
    allow_deterministic: bool = True
    #: Optional pre-built repository context (``ContextPack``) and evidence.
    context_pack: Any = None
    research_evidence: tuple = ()
    memory_records: tuple = ()
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

    def constraints_text(self) -> str:
        """Render the routing/generation constraints as a short instruction.

        Providers include this (when their API has no native slot for it) so
        the model invocation carries the constraints that were actually applied
        during routing, instead of dropping them at the provider boundary.
        """
        lines: list[str] = []
        capabilities = self.effective_capabilities()
        if capabilities:
            lines.append("required capabilities: " + ", ".join(capabilities))
        if self.min_context_window:
            lines.append(f"minimum context window: {self.min_context_window} tokens")
        if self.max_output_tokens is not None:
            lines.append(f"maximum output tokens: {self.max_output_tokens}")
        if self.max_latency_ms is not None:
            lines.append(f"maximum latency: {self.max_latency_ms} ms")
        if self.prefer_free is True:
            lines.append("prefer free provider")
        if self.prefer_local is True:
            lines.append("prefer local provider")
        return "\n".join(lines)

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
            # Session 11: addressing/governance metadata only — never content.
            "model": self.model,
            "backend": self.backend,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "timeout": self.timeout,
            "classification": self.classification,
            "network_policy": self.network_policy,
            "cost_budget_usd": self.cost_budget_usd,
            "hardware_profile": self.hardware_profile,
            "require_verified": self.require_verified,
            "allow_deterministic": self.allow_deterministic,
            "context_sections": (len(getattr(self.context_pack, "items", ()) or ())
                                 if self.context_pack is not None else 0),
            "research_evidence": len(self.research_evidence or ()),
            "memory_records": len(self.memory_records or ()),
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
