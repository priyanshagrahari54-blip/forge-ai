"""Forge Model Fabric: centralized model infrastructure.

Public API surface for capability-aware routing, provider/model registries,
structured requests/responses, telemetry, router feedback, credential handling,
and configuration.

:mod:`forge.models.runtime_bridge` is the opt-in adapter that lets the fabric
request inference through the Forge Native Model Runtime
(:mod:`forge.runtime.model_runtime`).  Nothing is wired up by default.
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
# Imported last: the bridge depends on forge.runtime.model_runtime, which is
# deliberately independent of this package (the dependency is one-way).
from forge.models.runtime_bridge import RuntimeProvider, attach_runtime

# -- Session 11: the real inference fabric -----------------------------------
#
# Identity, backends, verification, residency, routing, streaming, context
# budgeting, the fallback ladder and the request path itself. Nothing here is
# wired up by default: :func:`build_inference_fabric` and
# :func:`attach_inference` are explicit, opt-in entry points, so enabling the
# inference fabric can never silently change existing routing.
from forge.models.backends import (
    Backend,
    BackendError,
    BackendNotReadyError,
    BackendRegistry,
    BackendStatus,
    ForgeCustomBackend,
    LlamaCppCompatibleBackend,
    NativeLocalBackend,
    OllamaCompatibleBackend,
    RemoteProviderBackend,
    ResourceRequirements,
    RuntimeBackendAdapter,
)
from forge.models.catalog import CatalogError, DiscoveryReport, ModelCatalog
from forge.models.context_budget import (
    ContextBudgetPlanner,
    ContextPlan,
    ContextSection,
)
from forge.models.engine import (
    InferenceFabric,
    InferenceResult,
    InferenceStreamHandle,
    Observation,
    build_inference_fabric,
    scan_model_output,
)
from forge.models.evidence import (
    ROUTING_EVIDENCE_KINDS,
    evidence_to_findings,
    summarize_evidence,
)
from forge.models.fabric_bridge import (
    InferenceFabricProvider,
    attach_inference,
    detach_inference,
)
from forge.models.fallback import (
    DETERMINISTIC_MODEL_ID,
    FallbackLadder,
    FallbackPlan,
    FallbackStep,
    FallbackTier,
    TerminalState,
    classify_error,
)
from forge.models.identity import (
    AvailabilityState,
    IdentityError,
    MemoryRequirements,
    ModelIdentity,
    ModelSpoofingError,
    VerificationState,
)
from forge.models.model_cache import (
    ModelResidencyCache,
    ModelResidencyError,
    ResidencyEntry,
)
from forge.models.reference_engine import (
    ReferenceArtifactWriter,
    ReferenceLocalBackend,
    ReferenceModelConfig,
)
from forge.models.remote import (
    RemoteHttpBackend,
    RemoteProviderConfig,
    RemoteProviderStatus,
)
from forge.models.routing import (
    PolicyResult,
    ResourceResult,
    RoutingEngine,
    RoutingPlan,
    RoutingRequest,
    RoutingState,
)
from forge.models.streams import BoundedStream, StreamEvent, join_deltas
from forge.models.verification import (
    ModelVerifier,
    VerificationCheck,
    VerificationResult,
    fingerprint_artifact,
)
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
    "RuntimeProvider",
    "attach_runtime",
    # -- Session 11 -------------------------------------------------------
    "AvailabilityState",
    "VerificationState",
    "ModelIdentity",
    "MemoryRequirements",
    "IdentityError",
    "ModelSpoofingError",
    "Backend",
    "BackendStatus",
    "BackendRegistry",
    "BackendError",
    "BackendNotReadyError",
    "RuntimeBackendAdapter",
    "NativeLocalBackend",
    "OllamaCompatibleBackend",
    "LlamaCppCompatibleBackend",
    "ForgeCustomBackend",
    "RemoteProviderBackend",
    "ResourceRequirements",
    "RemoteHttpBackend",
    "RemoteProviderConfig",
    "RemoteProviderStatus",
    "ReferenceLocalBackend",
    "ReferenceArtifactWriter",
    "ReferenceModelConfig",
    "ModelCatalog",
    "CatalogError",
    "DiscoveryReport",
    "ModelVerifier",
    "VerificationResult",
    "VerificationCheck",
    "fingerprint_artifact",
    "ModelResidencyCache",
    "ModelResidencyError",
    "ResidencyEntry",
    "RoutingEngine",
    "RoutingRequest",
    "RoutingPlan",
    "RoutingState",
    "PolicyResult",
    "ResourceResult",
    "FallbackLadder",
    "FallbackPlan",
    "FallbackStep",
    "FallbackTier",
    "TerminalState",
    "classify_error",
    "DETERMINISTIC_MODEL_ID",
    "BoundedStream",
    "StreamEvent",
    "join_deltas",
    "ContextBudgetPlanner",
    "ContextPlan",
    "ContextSection",
    "InferenceFabric",
    "InferenceResult",
    "InferenceStreamHandle",
    "Observation",
    "build_inference_fabric",
    "scan_model_output",
    "InferenceFabricProvider",
    "attach_inference",
    "detach_inference",
    "ROUTING_EVIDENCE_KINDS",
    "evidence_to_findings",
    "summarize_evidence",
]
