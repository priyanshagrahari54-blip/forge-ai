"""Frontier specialist fleet for Forge.

The fleet contains logical specialist agents, not 1000 concurrent model
processes. Each specialist routes through the shared ModelFabric, so the same
real provider/model can serve many specialists on demand.
"""
from __future__ import annotations

from typing import Any

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.models.capabilities import ALL_CAPABILITIES
from forge.models.request import ModelRequest


def routing_capabilities(declared: tuple[str, ...]) -> tuple[str, ...]:
    """Map a specialist's declared labels onto canonical model capabilities.

    Labels such as ``web_research``, ``database`` or ``multilingual`` describe
    the *agent*, not a model: ``Model`` rejects unknown capabilities outright,
    so no provider can ever advertise them. Requiring them would make every
    routing attempt fail closed ("no registered model supports capabilities
    [...]") and leave the specialist registered but unexecutable. Only the
    canonical subset is required from a model; the declared labels stay
    attached to the specialist as metadata.
    """
    canonical = tuple(dict.fromkeys(
        label for label in declared if label in ALL_CAPABILITIES))
    return canonical or ("reasoning",)


def required_and_preferred(
        canonical: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split canonical capabilities into what is *required* and *preferred*.

    The router never relaxes a capability requirement, so everything listed as
    required must be advertised by the serving model or the specialist cannot
    run at all. Only the specialization-defining capability is therefore
    required — a coder needs ``coding`` and may *prefer* ``tool_use``, because
    tool execution belongs to Forge's own tool layer, not to the model. Making
    the preference a hard requirement left hundreds of specialists unroutable
    against perfectly capable models.
    """
    if not canonical:
        return ("reasoning",), ()
    return (canonical[0],), tuple(canonical[1:])


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
#: Every capability ``TaskRequirementExtractor`` can emit (coding, testing,
#: debugging, review, security, documentation, research, architecture,
#: performance, git) must be advertised — and be paired with the matching
#: role — by at least one specialization, otherwise the planner silently
#: drops that part of the requirement and the task runs under-covered.
#: Default output budget for one specialist call. Bounded on purpose: a
#: specialist answers or produces a snippet, and an unbounded request would let
#: a single agent consume an entire local runtime's capacity.
DEFAULT_MAX_OUTPUT_TOKENS = 512

#: Specialist work is engineering work: answer from the same facts twice, and
#: keep small-model rambling down.
DEFAULT_TEMPERATURE = 0.2

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
    ("performance", "performance",
     ("performance", "optimization", "coding")),
    ("refactor", "refactoring", ("coding", "refactor")),
    ("documentation", "documentation",
     ("documentation", "writing", "coding")),
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
    ("release", "release-engineering",
     ("git", "deployment", "testing")),
)


class FrontierModelAgentExecutor(AgentExecutor):
    """Routes one specialist persona through the shared ModelFabric."""

    def __init__(
        self,
        name: str,
        role: str,
        model_name: str,
        capabilities: tuple[str, ...],
        fabric: Any,
        *,
        declared_capabilities: tuple[str, ...] | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float | None = DEFAULT_TEMPERATURE,
    ) -> None:
        self.name = name
        self.role = role
        self.model_name = model_name
        self.declared_capabilities = tuple(
            declared_capabilities if declared_capabilities is not None
            else capabilities)
        #: What the Model Fabric is asked for: canonical capabilities only.
        self.capabilities = routing_capabilities(tuple(capabilities))
        #: Specialists are callers, not chat users: unbounded generation is a
        #: real cost/latency risk, so every specialist request carries a
        #: budget. A stop sequence still ends generation earlier.
        self.max_output_tokens = max(16, int(max_output_tokens))
        self.temperature = None if temperature is None else float(temperature)
        #: The router never relaxes a hard requirement, so only the
        #: specialization-defining capability is required; the rest travel as
        #: preferences that a capable model may satisfy.
        self.required_capabilities, self.preferred_capabilities = \
            required_and_preferred(self.capabilities)
        self.fabric = fabric

    def execute(self, request: AgentRequest) -> AgentResponse:
        context = ""
        if request.context is not None:
            context = "\n".join(
                f"{item.path}: {item.content}"
                for item in getattr(request.context, "items", ())
            )

        prompt = request.instructions or request.task.description
        #: Only what the model can act on. Routing facts (preferred model,
        #: capability requirements, fleet identity) travel in the request
        #: metadata where the router and the audit trail read them: restating
        #: them to the model wastes context and, on small instruct models,
        #: gets echoed back instead of an answer.
        model_prompt = (
            f"You are the {self.role} specialist on the Forge engineering team.\n"
            f"Task: {prompt}"
        )

        model_request = ModelRequest(
            prompt=model_prompt,
            task=self.role,
            caller=f"frontier-agent:{self.name}",
            context=context,
            #: Hard requirement: the capability that defines this
            #: specialization. A model that does not advertise it must not be
            #: used for this specialist.
            capability=self.required_capabilities[0],
            required_capabilities=self.required_capabilities,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            complexity=max(1.0, float(getattr(request.task, "attempts", 0) + 1)),
            metadata={
                "agent": self.name,
                "role": self.role,
                "preferred_model": self.model_name,
                "fleet": "frontier-1000-plus",
                #: Declared agent labels vs the canonical capabilities the
                #: model must actually support — never conflated.
                "declared_capabilities": ",".join(self.declared_capabilities),
                "routing_capabilities": ",".join(self.capabilities),
                "required_capabilities": ",".join(self.required_capabilities),
                "preferred_capabilities": ",".join(self.preferred_capabilities),
            },
        )
        response = self.fabric.generate(model_request)

        metadata = dict(getattr(response, "metadata", {}) or {})
        metadata.update({
            "preferred_model": self.model_name,
            "routed_model": getattr(response, "model", ""),
            "routed_provider": getattr(response, "provider", ""),
            "agent_role": self.role,
            "agent_capabilities": ",".join(self.capabilities),
            "declared_capabilities": ",".join(self.declared_capabilities),
            "fleet": "frontier-1000-plus",
        })
        return AgentResponse(
            success=bool(getattr(response, "success", False)),
            output=str(getattr(response, "text", "") or ""),
            error=str(getattr(response, "error", "") or ""),
            agent=self.name,
            stage=request.stage,
            context_fingerprint=request.context.fingerprint if request.context else "",
            metadata={str(k): str(v) for k, v in metadata.items()},
        )


def build_frontier_fleet(
    fabric: Any,
    *,
    minimum_size: int = 1000,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float | None = DEFAULT_TEMPERATURE,
) -> AgentRegistry:
    """Build 1,000+ logical specialists without spawning 1,000 processes.

    The registry is lightweight. Actual model inference happens only when a
    selected specialist executes a task.
    """
    target = max(1000, int(minimum_size))
    registrations: list[AgentRegistration] = []
    index = 0
    # Register in specialization-major order so every declared specialization
    # is represented before any specialist gets a second identity. Truncating
    # a fixed 26-variant sweep at an arbitrary target silently dropped the
    # trailing specializations (1,000 / 26 = 38.5, so only 38 of 40 roles
    # existed); the round-robin below guarantees full coverage at any size
    # while keeping every registered specialist executable.
    generation = 0
    while len(registrations) < target:
        for specialization, role, capabilities in SPECIALIZATIONS:
            if len(registrations) >= target:
                break
            name = f"{specialization}-{generation + 1:02d}-{index + 1:04d}"
            model_name = FRONTIER_MODELS[
                (generation + index) % len(FRONTIER_MODELS)
            ]
            #: The registry keeps the specialist's declared labels so
            #: agent-level selection (``get_by_capability``) still works; the
            #: executor canonicalises them for the model request, which is
            #: what makes every registered specialist routable.
            registrations.append(
                AgentRegistration(
                    name,
                    role,
                    FrontierModelAgentExecutor(
                        name, role, model_name, tuple(capabilities), fabric,
                        declared_capabilities=tuple(capabilities),
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                    ),
                    tuple(capabilities),
                )
            )
            index += 1
        generation += 1
    return AgentRegistry(registrations)


def extend_registry_with_frontier_fleet(
    registry: AgentRegistry,
    fabric: Any,
    *,
    minimum_size: int = 1000,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float | None = DEFAULT_TEMPERATURE,
) -> AgentRegistry:
    """Add the fleet to an existing registry while preserving core agents."""
    fleet = build_frontier_fleet(fabric, minimum_size=minimum_size,
                                 max_output_tokens=max_output_tokens,
                                 temperature=temperature)
    for name in fleet.names():
        registration = fleet.get(name)
        registry.register(registration)
    return registry
