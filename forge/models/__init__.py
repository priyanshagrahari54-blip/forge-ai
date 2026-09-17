"""Forge Model Fabric: centralized model infrastructure.

Public API surface for capability-aware routing, provider/model registries,
structured requests/responses, telemetry, router feedback, credential handling,
and configuration.
"""
from forge.models.capabilities import (
    AGENTIC_CAPABILITIES, ALL_CAPABILITIES, AUDIO_CAPABILITIES,
    MULTIMODAL_CAPABILITIES, TEXT_CAPABILITIES, Capability,
    is_capability, normalize_capability,
)
from forge.models.config import FabricConfig
from forge.models.consensus import ConsensusResult, ConsensusStrategy, consensus
from forge.models.credentials import CredentialError, CredentialStore
from forge.models.errors import CapabilityNotSupportedError, ConfigurationError, FabricError, ModelUnavailableError, ProviderError
from forge.models.fabric import ModelFabric
from forge.models.feedback import RouterFeedback
from forge.models.health import HealthStatus, ModelHealth
from forge.models.policy import DEFAULT_FALLBACK_ORDER, RoutingPolicy
from forge.models.readiness import ReadinessCheck, ReadinessReport, check_fabric_readiness, describe_no_model_error, fabric_has_real_model, is_fallback_response
from forge.models.provider import LocalModelProvider, MockProvider, ModelProvider, ModelResult, OllamaProvider, OpenAIProvider, Provider, ProviderInfo, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest, ModelResponse
from forge.models.runtime_bridge import RuntimeProvider, attach_runtime
from forge.models.backends import Backend, BackendError, BackendNotReadyError, BackendRegistry, BackendStatus, ForgeCustomBackend, LlamaCppCompatibleBackend, NativeLocalBackend, OllamaCompatibleBackend, RemoteProviderBackend, ResourceRequirements, RuntimeBackendAdapter
from forge.models.catalog import CatalogError, DiscoveryReport, ModelCatalog
from forge.models.context_budget import ContextBudgetPlanner, ContextPlan, ContextSection
from forge.models.engine import InferenceFabric, InferenceResult, InferenceStreamHandle, Observation, build_inference_fabric, scan_model_output
from forge.models.evidence import ROUTING_EVIDENCE_KINDS, evidence_to_findings, summarize_evidence
from forge.models.fabric_bridge import InferenceFabricProvider, attach_inference, detach_inference
from forge.models.inference_path import PATH_HYBRID, PATH_LEGACY, PATH_SESSION11, ExecutionIdentity, IdentityBoundFabric, InferencePathConfig, PathDecision, Session11Adapter, decide_path, provenance_from_response
from forge.models.fallback import DETERMINISTIC_MODEL_ID, FallbackLadder, FallbackPlan, FallbackStep, FallbackTier, TerminalState, classify_error
from forge.models.identity import AvailabilityState, IdentityError, MemoryRequirements, ModelIdentity, ModelSpoofingError, VerificationState
from forge.models.model_cache import ModelResidencyCache, ModelResidencyError, ResidencyEntry
from forge.models.model_studio import BenchmarkCase, BenchmarkResult, DatasetIssue, DatasetReport, ModelArtifact, ModelProvenance, ModelStudio, PromotionGate, SpecializationProfile, TrainingBackend, TrainingPlan, build_sft_record, stable_split
from forge.models.builtin_profiles import FORGE_CODING, FORGE_DEBUG, FORGE_SECURITY, FORGE_WEB, FORGE_GAME3D, FORGE_PROFILES
from forge.models.hf_trainer import HuggingFacePEFTBackend
from forge.models.reference_engine import ReferenceArtifactWriter, ReferenceLocalBackend, ReferenceModelConfig
from forge.models.remote import RemoteHttpBackend, RemoteProviderConfig, RemoteProviderStatus
from forge.models.routing import PolicyResult, ResourceResult, RoutingEngine, RoutingPlan, RoutingRequest, RoutingState
from forge.models.streams import BoundedStream, StreamEvent, join_deltas
from forge.models.verification import ModelVerifier, VerificationCheck, VerificationResult, fingerprint_artifact
from forge.models.router import FabricRouter, ModelInfo, ModelRouter, RouteDecision, RoutingDecision
from forge.models.telemetry import Telemetry, TelemetryEvent
from forge.models.model_catalog import ModelDescriptor, PROVIDER_ECOSYSTEMS, catalog, catalog_snapshot
from forge.models.oss_registry import OSSModel, VERIFIED_SEEDS, discover_huggingface_models, oss_catalog, oss_catalog_snapshot

__all__ = [
    "Capability", "ALL_CAPABILITIES", "TEXT_CAPABILITIES", "MULTIMODAL_CAPABILITIES", "AUDIO_CAPABILITIES", "AGENTIC_CAPABILITIES", "is_capability", "normalize_capability",
    "Model", "ModelRegistry", "ModelHealth", "HealthStatus", "ModelProvider", "Provider", "ProviderInfo", "ProviderRegistry", "ModelResult", "LocalModelProvider", "OllamaProvider", "OpenAIProvider", "MockProvider", "ModelRequest", "ModelResponse", "ModelInfo", "RoutingDecision", "ModelRouter", "RouteDecision", "FabricRouter", "RoutingPolicy", "DEFAULT_FALLBACK_ORDER", "ReadinessCheck", "ReadinessReport", "check_fabric_readiness", "describe_no_model_error", "fabric_has_real_model", "is_fallback_response", "Telemetry", "TelemetryEvent", "RouterFeedback", "CredentialStore", "CredentialError", "FabricConfig", "FabricError", "ModelUnavailableError", "CapabilityNotSupportedError", "ProviderError", "ConfigurationError", "ModelFabric", "ConsensusStrategy", "ConsensusResult", "consensus", "RuntimeProvider", "attach_runtime",
    "AvailabilityState", "VerificationState", "ModelIdentity", "MemoryRequirements", "IdentityError", "ModelSpoofingError", "Backend", "BackendStatus", "BackendRegistry", "BackendError", "BackendNotReadyError", "RuntimeBackendAdapter", "NativeLocalBackend", "OllamaCompatibleBackend", "LlamaCppCompatibleBackend", "ForgeCustomBackend", "RemoteProviderBackend", "ResourceRequirements", "RemoteHttpBackend", "RemoteProviderConfig", "RemoteProviderStatus", "ReferenceLocalBackend", "ReferenceArtifactWriter", "ReferenceModelConfig", "ModelCatalog", "CatalogError", "DiscoveryReport", "ModelVerifier", "VerificationResult", "VerificationCheck", "fingerprint_artifact", "ModelResidencyCache", "ModelResidencyError", "ResidencyEntry", "RoutingEngine", "RoutingRequest", "RoutingPlan", "RoutingState", "PolicyResult", "ResourceResult", "FallbackLadder", "FallbackPlan", "FallbackStep", "FallbackTier", "TerminalState", "classify_error", "DETERMINISTIC_MODEL_ID", "BoundedStream", "StreamEvent", "join_deltas", "ContextBudgetPlanner", "ContextPlan", "ContextSection", "InferenceFabric", "InferenceResult", "InferenceStreamHandle", "Observation", "build_inference_fabric", "scan_model_output", "InferenceFabricProvider", "attach_inference", "detach_inference", "PATH_HYBRID", "PATH_LEGACY", "PATH_SESSION11", "ExecutionIdentity", "IdentityBoundFabric", "InferencePathConfig", "PathDecision", "Session11Adapter", "decide_path", "provenance_from_response", "ROUTING_EVIDENCE_KINDS", "evidence_to_findings", "summarize_evidence",
    "ModelStudio", "SpecializationProfile", "TrainingPlan", "TrainingBackend", "ModelArtifact", "ModelProvenance", "DatasetIssue", "DatasetReport", "BenchmarkCase", "BenchmarkResult", "PromotionGate", "build_sft_record", "stable_split", "FORGE_CODING", "FORGE_DEBUG", "FORGE_SECURITY", "FORGE_WEB", "FORGE_GAME3D", "FORGE_PROFILES", "HuggingFacePEFTBackend", "ModelDescriptor", "PROVIDER_ECOSYSTEMS", "catalog", "catalog_snapshot", "OSSModel", "VERIFIED_SEEDS", "discover_huggingface_models", "oss_catalog", "oss_catalog_snapshot",
]
