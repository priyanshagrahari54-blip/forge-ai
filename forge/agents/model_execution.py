"""Agent -> verified Model Fabric execution with provider failover."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from forge.agents.fleet import AgentSlot
from forge.agents.routing import model_request_for_agent
from forge.models.request import ModelRequest
from forge.models.registry import ModelRegistry
from forge.models.router import FabricRouter, RouteDecision
from forge.orchestration.failover import FailoverPool


@dataclass(frozen=True)
class AgentModelAttempt:
    agent: str
    model: str
    provider: str
    success: bool
    reason: str = ""


@dataclass
class AgentModelResult:
    success: bool
    value: Any = None
    request: ModelRequest | None = None
    selected_model: str = ""
    selected_provider: str = ""
    attempts: list[AgentModelAttempt] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "attempts": [a.__dict__.copy() for a in self.attempts],
            "route": dict(self.route),
            "error": self.error,
        }


class AgentModelExecutor:
    """Bridge logical specialist agents to runtime-verified models.

    Model Fabric chooses the ordered candidate chain. This layer removes
    candidates that are not currently runtime-verified and delegates transient
    failures to the existing quota-aware FailoverPool.
    """

    def __init__(self, registry: ModelRegistry, *, router: FabricRouter | None = None,
                 failover: FailoverPool | None = None) -> None:
        self.registry = registry
        self.router = router or FabricRouter(registry=registry)
        self.failover = failover or FailoverPool()

    def execute(
        self,
        slot: AgentSlot,
        *,
        operation: Callable[[str, str, ModelRequest], Any],
        prompt: str = "",
        task: str = "",
        caller: str = "",
        request_kwargs: dict[str, Any] | None = None,
    ) -> AgentModelResult:
        request = model_request_for_agent(
            slot, prompt=prompt, task=task, caller=caller, **(request_kwargs or {})
        )
        decision = self.router.route(request)
        candidates = self._eligible_candidates(decision)
        if not candidates:
            return AgentModelResult(
                success=False, request=request, route=decision.to_dict(),
                error=decision.error or "no runtime-verified model candidate",
            )

        attempts: list[AgentModelAttempt] = []

        def invoke(model_name: str, provider: str) -> Any:
            try:
                value = operation(model_name, provider, request)
            except Exception as exc:
                reason = getattr(exc, "reason", None) or "transient"
                attempts.append(AgentModelAttempt(slot.name, model_name, provider, False, reason))
                raise
            attempts.append(AgentModelAttempt(slot.name, model_name, provider, True))
            return value

        outcome = self.failover.run(
            ((model.name, model.provider) for model in candidates), invoke
        )
        winner = next((a for a in reversed(attempts) if a.success), None)
        if winner is not None:
            return AgentModelResult(
                success=True, value=outcome.value, request=request,
                selected_model=winner.model, selected_provider=winner.provider,
                attempts=attempts, route=decision.to_dict(),
            )
        return AgentModelResult(
            success=False, request=request,
            selected_model=decision.model.name if decision.model else "",
            selected_provider=decision.model.provider if decision.model else "",
            attempts=attempts, route=decision.to_dict(),
            error="all runtime-verified model candidates failed",
        )

    def _eligible_candidates(self, decision: RouteDecision) -> list[Any]:
        names = decision.candidates or ((decision.model.name,) if decision.model else ())
        result = []
        for name in names:
            if not self.registry.has(name):
                continue
            model = self.registry.get(name)
            if not model.available or model.health.last_resort:
                continue
            if model.metadata.get("runtime_verified") is not True:
                continue
            result.append(model)
        return result
