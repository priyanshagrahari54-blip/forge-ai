"""Frontier specialist fleet for Forge.

The fleet contains logical specialist agents, not 1,040 concurrent model
processes. Each specialist routes through the shared ModelFabric, so the same
real provider/model can serve many specialists on demand, and each specialist
may run on *any* eligible model: eligibility is resolved from the fabric's
live registry at execution time (available, inference-verified, advertising
the required capability), never from a hardcoded model list.

Fleet sizing: 40 specialization families x 26 variants = 1,040 logical agents
by default. A variant is a deterministic preference posture over the eligible
models (variant k favours the k-th eligible model, then fails over along the
rest), which spreads a family's work across every model that can do it.
Callers may build a smaller fleet for tests or verification scripts; the
production fleet is built with the default.
"""
from __future__ import annotations

from typing import Any

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.models.capabilities import ALL_CAPABILITIES
from forge.models.request import ModelRequest


#: 40 families x 26 variants = the canonical production fleet size.
SPECIALISTS_PER_FAMILY = 26
DEFAULT_FLEET_SIZE = SPECIALISTS_PER_FAMILY * 40


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


#: Fleet label carried in request/response metadata (40 families x 26 variants).
FLEET_LABEL = "frontier-1040"

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


def eligible_models(fabric: Any, required: tuple[str, ...], *,
                    variant: int = 1) -> tuple[str, ...]:
    """Registered models that can serve ``required`` right now, best first.

    Eligibility is read from the fabric's live registry — never from a
    hardcoded list: a model qualifies when it is ``available`` (a real
    inference verdict, see :mod:`forge.models.runtime_verification`), is not
    the deterministic fallback rung, and advertises every required
    capability. Inference-verified models rank ahead of merely configured
    ones. ``variant`` rotates each group deterministically so a family's 26
    variants spread their work across all eligible models instead of all
    preferring the same one; the router still fails over along the rest.
    """
    registry = getattr(fabric, "registry", None)
    if registry is None:
        return ()
    try:
        indexed = getattr(registry, "models_for_capabilities", None)
        models = list(indexed(required)) if callable(indexed) else [
            model for model in registry if model.supports_all(required)]
    except Exception:  # noqa: BLE001 - a duck-typed fabric proves nothing
        return ()
    usable = [model for model in models
              if getattr(model, "available", False) and not getattr(model, "fallback", False)]
    verified = [model.name for model in usable
                if (getattr(model, "metadata", None) or {}).get("runtime_verified") is True]
    unverified = [model.name for model in usable if model.name not in verified]

    def rotate(names: list[str]) -> list[str]:
        if len(names) < 2:
            return names
        offset = (max(1, int(variant)) - 1) % len(names)
        return names[offset:] + names[:offset]

    return tuple(rotate(verified) + rotate(unverified))


class FrontierModelAgentExecutor(AgentExecutor):
    """Routes one specialist persona through the shared ModelFabric.

    A specialist is a logical role, not a model binding: the models it may
    run on are resolved at execution time from the fabric registry
    (:func:`eligible_models`). ``model_name`` is an optional *explicit*
    preference for callers that already hold a real registered model id (the
    multimodal fleet passes the modality model it found); it is never
    invented here.
    """

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
        variant: int = 1,
    ) -> None:
        self.name = name
        self.role = role
        self.model_name = model_name or ""
        self.variant = max(1, int(variant))
        #: Rich specialization labels stay on the agent: they are identity,
        #: prompting and observability metadata — never routing requirements.
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

    @property
    def specialization_tags(self) -> tuple[str, ...]:
        """Alias for the declared specialization labels (routing metadata)."""
        return self.declared_capabilities

    def eligible_models(self) -> tuple[str, ...]:
        """Models that can serve this specialist right now (see module doc)."""
        return eligible_models(self.fabric, self.required_capabilities,
                               variant=self.variant)

    def preferred_models(self) -> tuple[str, ...]:
        """Explicit preference (if any) followed by the live eligible models."""
        explicit = (self.model_name,) if self.model_name else ()
        return tuple(dict.fromkeys(explicit + self.eligible_models()))

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
        #: gets echoed back instead of an answer. The declared labels are
        #: still surfaced to the model once so the persona is steerable.
        model_prompt = (
            f"You are the {self.role} specialist on the Forge engineering team "
            f"({', '.join(self.declared_capabilities)}).\n"
            f"Task: {prompt}"
        )

        preferred = self.preferred_models()
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
            #: Soft ordering hint only: the eligible models travel as a
            #: request-level preference, so the variant's first choice can
            #: win *among otherwise eligible candidates* but can never bypass
            #: capability, health, policy, or runtime-verification gates.
            preferred_models=preferred,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            complexity=max(1.0, float(getattr(request.task, "attempts", 0) + 1)),
            metadata={
                "agent": self.name,
                "role": self.role,
                "variant": str(self.variant),
                "preferred_model": preferred[0] if preferred else "",
                "eligible_models": ",".join(preferred),
                "fleet": FLEET_LABEL,
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
            "preferred_model": preferred[0] if preferred else "",
            "eligible_models": ",".join(preferred),
            "routed_model": getattr(response, "model", ""),
            "routed_provider": getattr(response, "provider", ""),
            "agent_role": self.role,
            "agent_variant": str(self.variant),
            "agent_capabilities": ",".join(self.capabilities),
            "declared_capabilities": ",".join(self.declared_capabilities),
            "fleet": FLEET_LABEL,
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
    minimum_size: int = DEFAULT_FLEET_SIZE,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float | None = DEFAULT_TEMPERATURE,
) -> AgentRegistry:
    """Build the logical specialist fleet without spawning any processes.

    The registry is lightweight. Actual model inference happens only when a
    selected specialist executes a task. The default builds the canonical
    40 families x 26 variants = 1,040 logical specialists; ``minimum_size``
    lets verification scripts and tests build a smaller fleet, never fewer
    than one variant per specialization.
    """
    target = max(len(SPECIALIZATIONS), int(minimum_size))
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
            #: The registry keeps the specialist's declared labels so
            #: agent-level selection (``get_by_capability``) still works; the
            #: executor canonicalises them for the model request, which is
            #: what makes every registered specialist routable. No model is
            #: bound here: eligibility is resolved from the fabric at run time.
            registrations.append(
                AgentRegistration(
                    name,
                    role,
                    FrontierModelAgentExecutor(
                        name, role, "", tuple(capabilities), fabric,
                        declared_capabilities=tuple(capabilities),
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                        variant=generation + 1,
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
    minimum_size: int = DEFAULT_FLEET_SIZE,
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
