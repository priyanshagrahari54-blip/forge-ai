"""Frontier specialist fleet for Forge.

The fleet contains logical specialist agents, not 1000 concurrent model
processes. Each specialist routes through the shared ModelFabric, so the same
real provider/model can serve many specialists on demand.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.models.request import ModelRequest


FRONTIER_MODELS: tuple[str, ...] = (
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.4",
    "openai/gpt-4.1",
    "openai/gpt-4o",
    "anthropic/claude-opus-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-opus-4-7",
    "anthropic/claude-opus-4-6",
    "anthropic/claude-opus-4-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-sonnet-4-5",
)

# 40 specializations x 26 model/strategy variants = 1,040 logical agents.
SPECIALIZATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("planner", "planning", ("planning", "reasoning")),
    ("architect", "architecture", ("architecture", "reasoning")),
    ("researcher", "research", ("research", "web_research")),
    ("coder", "coding", ("coding", "tool_use")),
    ("frontend", "frontend", ("coding", "web")),
    ("backend", "backend", ("coding", "api")),
    ("database", "database", ("coding", "database")),
    ("devops", "devops", ("coding", "deployment")),
    ("debugger", "debugging", ("debugging", "coding")),
    ("tester", "testing", ("testing", "coding")),
    ("reviewer", "reviewing", ("review", "reasoning")),
    ("security", "security", ("security", "reasoning")),
    ("performance", "performance", ("optimization", "coding")),
    ("refactor", "refactoring", ("coding", "refactor")),
    ("documentation", "documentation", ("writing", "coding")),
    ("api", "api-design", ("coding", "api")),
    ("data", "data-engineering", ("coding", "data")),
    ("ml", "machine-learning", ("coding", "reasoning")),
    ("vision", "vision", ("vision", "multimodal")),
    ("audio", "audio", ("audio", "multimodal")),
    ("game", "game-development", ("coding", "game")),
    ("os", "os-engineering", ("coding", "systems")),
    ("browser", "browser", ("browser", "web")),
    ("computer-use", "computer-use", ("computer_use", "tool_use")),
    ("ux", "ux", ("design", "reasoning")),
    ("product", "product", ("planning", "research")),
    ("qa", "quality-assurance", ("testing", "review")),
    ("compliance", "compliance", ("security", "review")),
    ("privacy", "privacy", ("security", "review")),
    ("localization", "localization", ("writing", "multilingual")),
    ("math", "mathematics", ("reasoning", "math")),
    ("science", "science", ("reasoning", "research")),
    ("finance", "finance", ("reasoning", "data")),
    ("legal", "legal-analysis", ("research", "reasoning")),
    ("prompt", "prompt-engineering", ("reasoning", "writing")),
    ("agentic", "agentic-systems", ("reasoning", "tool_use")),
    ("memory", "memory", ("reasoning", "data")),
    ("orchestration", "orchestration", ("planning", "tool_use")),
    ("integration", "integration", ("coding", "tool_use")),
    ("release", "release-engineering", ("deployment", "testing")),
)


class FrontierModelAgentExecutor(AgentExecutor):
    """Routes one specialist persona through the shared ModelFabric."""

    def __init__(self, name: str, role: str, model_name: str, fabric: Any) -> None:
        self.name = name
        self.role = role
        self.model_name = model_name
        self.fabric = fabric

    def execute(self, request: AgentRequest) -> AgentResponse:
        context = ""
        if request.context is not None:
            context = "\n".join(
                f"{item.path}: {item.content}"
                for item in getattr(request.context, "items", ())
            )
        prompt = request.instructions or request.task.description
        prompt = (
            f"You are Forge specialist {self.name} ({self.role}). "
            f"Route through the shared ModelFabric. Preferred model: {self.model_name}. "
            "Do not claim tool execution you did not perform.\n\n"
            + prompt
        )
        model_request = ModelRequest(
            prompt=prompt,
            task=self.role,
            context=context,
            capability="reasoning",
            metadata={"agent": self.name, "preferred_model": self.model_name},
        )
        response = self.fabric.generate(model_request)
        metadata = dict(getattr(response, "metadata", {}) or {})
        metadata["preferred_model"] = self.model_name
        metadata["routed_model"] = getattr(response, "model", "")
        metadata["routed_provider"] = getattr(response, "provider", "")
        return AgentResponse(
            success=bool(getattr(response, "success", False)),
            output=str(getattr(response, "text", "") or ""),
            error=str(getattr(response, "error", "") or ""),
            agent=self.name,
            stage=request.stage,
            context_fingerprint=request.context.fingerprint if request.context else "",
            metadata={str(k): str(v) for k, v in metadata.items()},
        )


def build_frontier_fleet(fabric: Any, *, minimum_size: int = 1000) -> AgentRegistry:
    """Build 1,000+ logical specialists without spawning 1,000 processes.

    The registry is lightweight. Actual model inference happens only when a
    selected specialist executes a task.
    """
    target = max(1000, int(minimum_size))
    registrations: list[AgentRegistration] = []
    index = 0
    while len(registrations) < target:
        for specialization, role, capabilities in SPECIALIZATIONS:
            for variant in range(26):
                if len(registrations) >= target:
                    break
                model_name = FRONTIER_MODELS[
                    (variant + index) % len(FRONTIER_MODELS)
                ]
                name = f"{specialization}-{variant + 1:02d}-{index + 1:04d}"
                registrations.append(
                    AgentRegistration(
                        name,
                        role,
                        FrontierModelAgentExecutor(name, role, model_name, fabric),
                        tuple(capabilities),
                    )
                )
                index += 1
    return AgentRegistry(registrations)


def extend_registry_with_frontier_fleet(
    registry: AgentRegistry, fabric: Any, *, minimum_size: int = 1000
) -> AgentRegistry:
    """Add the fleet to an existing registry while preserving core agents."""
    fleet = build_frontier_fleet(fabric, minimum_size=minimum_size)
    for name in fleet.names():
        registration = fleet.get(name)
        registry.register(registration)
    return registry
