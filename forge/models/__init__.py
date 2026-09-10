"""Forge Model Fabric: centralized model infrastructure.

Public API surface for capability-aware routing, provider/model registries,
structured requests/responses, telemetry, router feedback, credential handling,
and configuration.
"""
from forge.models.capabilities import (
    AGENTIC_CAPABILITIES,
    ALL_CAPABILITIES,
    AUDIO_CAPABILITIES,
    MULTIMODAL_CAPABILITIES,
    TEXT_CAPABILITIES,
    Capability,
    is_capability,
    normalize_capability,
)
from forge.models.config import FabricConfig
from forge.models.consensus import ConsensusResult, ConsensusStrategy, consensus
from forge.models.credentials import CredentialError, CredentialStore
from forge.models.errors import (
    CapabilityNotSupportedError,
    ConfigurationError,
    FabricError,
    ModelUnavailableError,
    ProviderError,
)
from forge.models.fabric import ModelFabric
from forge.models.feedback import RouterFeedback
from forge.models.health import HealthStatus, ModelHealth
from forge.models.policy import DEFAULT_FALLBACK_ORDER, RoutingPolicy
from forge.models.readiness import (
    ReadinessCheck,
    ReadinessReport,
    check_fabric_readiness,
    describe_no_model_error,
    fabric_has_real_model,
    is_fallback_response,
)
from forge.models.provider import (
    LocalModelProvider,
    MockProvider,
    ModelProvider,
    ModelResult,
    OllamaProvider,
    OpenAIProvider,
    Provider,
    ProviderInfo,
    ProviderRegistry,
)
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest, ModelResponse
from forge.models.router import (
    FabricRouter,
    ModelInfo,
    ModelRouter,
    RouteDecision,
    RoutingDecision,
)
from forge.models.telemetry import Telemetry, TelemetryEvent

__all__ = [
    "Capability",
    "ALL_CAPABILITIES",
    "TEXT_CAPABILITIES",
    "MULTIMODAL_CAPABILITIES",
    "AUDIO_CAPABILITIES",
    "AGENTIC_CAPABILITIES",
    "is_capability",
    "normalize_capability",
    "Model",
    "ModelRegistry",
    "ModelHealth",
    "HealthStatus",
    "ModelProvider",
    "Provider",
    "ProviderInfo",
    "ProviderRegistry",
    "ModelResult",
    "LocalModelProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "MockProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelInfo",
    "RoutingDecision",
    "ModelRouter",
    "RouteDecision",
    "FabricRouter",
    "RoutingPolicy",
    "DEFAULT_FALLBACK_ORDER",
    "ReadinessCheck",
    "ReadinessReport",
    "check_fabric_readiness",
    "describe_no_model_error",
    "fabric_has_real_model",
    "is_fallback_response",
    "Telemetry",
    "TelemetryEvent",
    "RouterFeedback",
    "CredentialStore",
    "CredentialError",
    "FabricConfig",
    "FabricError",
    "ModelUnavailableError",
    "CapabilityNotSupportedError",
    "ProviderError",
    "ConfigurationError",
    "ModelFabric",
    "ConsensusStrategy",
    "ConsensusResult",
    "consensus",
]
