"""Bridge from the Model Fabric to the Session 11 inference fabric.

Agents already call :class:`~forge.models.fabric.ModelFabric`. This module is
the seam that makes those calls travel the canonical chain without touching a
single agent::

    Agent -> ModelFabric -> InferenceFabricProvider (this module)
        -> InferenceFabric -> RoutingEngine -> ModelCatalog
            -> Backend -> ModelRuntime -> ModelBackend -> model

It is **opt-in and additive**: ``ModelFabric.from_defaults()`` is unchanged, so
enabling the inference fabric can never silently alter existing routing. When
attached, the provider is registered like any other, the catalog's verified
identities are mirrored into the fabric registry, and every response is
translated back into the fabric's ``ModelResult`` vocabulary.

Honesty rules, inherited from the engine and re-checked here:

* a failure stays a failure — nothing is synthesized to fill a gap;
* a deterministic (non-neural) answer is labelled as such in the metadata;
* an unverified model is never presented as ready.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from forge.models.capabilities import TEXT_CAPABILITIES
from forge.models.identity import ModelIdentity
from forge.models.provider import ModelResult, ProviderInfo
from forge.models.registry import Model

__all__ = ["InferenceFabricProvider", "attach_inference", "detach_inference"]


class InferenceFabricProvider:
    """A fabric provider whose inference goes through the InferenceFabric."""

    def __init__(self, inference: Any, *, name: str = "inference",
                 capability: str = "") -> None:
        if inference is None:
            raise ValueError("an InferenceFabric instance is required")
        self.inference = inference
        self.name = name or "inference"
        self.capability = capability

    # -- fabric provider contract ----------------------------------------

    def _request(self, prompt: str, *, context: str, task: str,
                 instructions: str, max_output_tokens: Optional[int],
                 temperature: Optional[float], capability: str) -> Any:
        from forge.models.request import ModelRequest

        return ModelRequest(
            prompt=prompt or "", context=context or "", task=task or "",
            capability=capability or self.capability or "coding",
            max_output_tokens=max_output_tokens, temperature=temperature,
            metadata={"instructions": (instructions or "")[:4000]})

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "",
                 max_output_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 capability: str = "") -> ModelResult:
        request = self._request(prompt, context=context, task=task,
                                instructions=instructions,
                                max_output_tokens=max_output_tokens,
                                temperature=temperature,
                                capability=capability)
        result = self.inference.generate(request)
        if not getattr(result, "success", False):
            state = str(getattr(result, "state", "failed"))
            code = str(getattr(result, "error_code", "") or "")
            raise RuntimeError(
                "inference fabric could not serve the request (%s%s): %s"
                % (state, ("/" + code) if code else "",
                   str(getattr(result, "error", "") or "no detail")[:400]))
        metadata: Dict[str, Any] = {
            "backend_id": getattr(result, "backend_id", ""),
            "inference_model_id": getattr(result, "model_id", ""),
            "state": getattr(result, "state", ""),
            "finish_reason": getattr(result, "finish_reason", ""),
            "neural": bool(getattr(result, "neural", True)),
            #: The one distinction an agent must never lose: was this text
            #: produced by a model, or by the deterministic non-neural rung?
            "deterministic": not bool(getattr(result, "neural", True)),
            "generation_id": getattr(result, "generation_id", ""),
            "request_id": getattr(result, "request_id", ""),
            "verification_state": getattr(result, "verification_state", ""),
            "availability_state": getattr(result, "availability_state", ""),
            "output_flags": list(
                (getattr(result, "output_scan", {}) or {}).get("flags") or ()),
        }
        return ModelResult(
            getattr(result, "text", "") or "",
            getattr(result, "model_id", "") or self.name,
            input_tokens=int(getattr(result, "input_tokens", 0) or 0),
            output_tokens=int(getattr(result, "output_tokens", 0) or 0),
            latency=(float(getattr(result, "latency_ms", 0.0) or 0.0) / 1000.0),
            metadata=metadata,
        )

    def stream(self, prompt: str, *, context: str = "", task: str = "",
               instructions: str = "",
               max_output_tokens: Optional[int] = None,
               temperature: Optional[float] = None,
               capability: str = "") -> Iterator[str]:
        request = self._request(prompt, context=context, task=task,
                                instructions=instructions,
                                max_output_tokens=max_output_tokens,
                                temperature=temperature,
                                capability=capability)
        handle = self.inference.stream(request)
        for event in handle.events():
            delta = getattr(event, "delta", "")
            if delta:
                yield delta
        final = handle.wait(1.0)
        if not getattr(final, "success", False) and getattr(final, "error", ""):
            raise RuntimeError("inference stream failed: %s"
                               % str(final.error)[:400])

    def list_models(self) -> List[str]:
        try:
            payload = self.inference.models_list()
            return [str(item.get("model_id") or "")
                    for item in payload.get("models") or []]
        except Exception:
            return []

    def health(self) -> Dict[str, Any]:
        try:
            payload = self.inference.status()
        except Exception as exc:
            return {"available": False, "error": str(exc)[:300]}
        backends = payload.get("backends") or []
        ready = [item.get("backend_id") for item in backends
                 if item.get("ready")]
        models = (payload.get("models") or {})
        return {
            "available": bool(ready or int(models.get("usable") or 0)),
            "ready_backends": ready,
            "usable_models": int(models.get("usable") or 0),
            "verified_models": int(models.get("verified") or 0),
            "in_flight": int(payload.get("in_flight") or 0),
            "counts": dict(payload.get("counts") or {}),
        }


def attach_inference(fabric: Any, inference: Any, *,
                     name: str = "inference",
                     register_models: bool = True,
                     capabilities: Tuple[str, ...] = TEXT_CAPABILITIES,
                     verified_only: bool = True,
                     context_window: int = 8192,
                     max_output_tokens: int = 2048) -> List[str]:
    """Register an inference-fabric provider (and its models) with a fabric.

    Returns the fabric model names that were registered. Mirroring is
    deliberately conservative: only identities the catalog has actually
    *verified* are registered by default, and a model that advertises no
    capability (like the reference engine) is registered with an empty
    capability set so capability routing cannot select it for real work.
    """
    if fabric is None:
        raise ValueError("a ModelFabric instance is required")
    if inference is None:
        raise ValueError("an InferenceFabric instance is required")

    provider = InferenceFabricProvider(inference, name=name)
    fabric.register_provider(
        name, provider,
        ProviderInfo(name=name, display_name="Forge Inference Fabric",
                     kind="local", local=True, free=True,
                     capabilities=tuple(capabilities), model=""))

    registered: List[str] = []
    if not register_models:
        return registered
    try:
        identities: List[ModelIdentity] = inference.catalog.list()
    except Exception:
        return registered
    for identity in identities:
        if verified_only and not identity.verified:
            continue
        fabric_name = "%s/%s" % (name, identity.name)
        if fabric.registry.has(fabric_name):
            continue
        advertised = tuple(identity.capabilities)
        try:
            fabric.register_model(Model(
                name=fabric_name, provider=name,
                capabilities=advertised,
                capability_status={item: "verified" for item in advertised},
                context_window=int(identity.context_limit or context_window),
                max_output_tokens=int(identity.max_output_limit
                                      or max_output_tokens),
                free=bool(identity.free), local=bool(identity.local),
                cost_per_token=float(identity.cost_per_token or 0.0),
                latency_ms=float(identity.latency_ms or 0.0),
                reliability=float(identity.reliability if identity.reliability
                                  else 1.0),
                metadata={
                    "inference_model_id": identity.model_id,
                    "inference_backend_id": identity.backend_id,
                    "availability_state": identity.availability_state,
                    "verification_state": identity.verification_state,
                    "artifact_fingerprint": identity.artifact_fingerprint,
                    "reference_engine": bool(
                        identity.metadata.get("reference_engine")),
                    "neural": bool(identity.metadata.get("neural", True)),
                    "streaming": True,
                    "description": ("Served through the Forge inference "
                                    "fabric (Session 11)."),
                }))
        except ValueError:
            continue
        registered.append(fabric_name)
    return registered


def detach_inference(fabric: Any, name: Any = "inference") -> bool:
    """Remove a previously attached provider and its mirrored models.

    ``name`` is either the provider name (the default) or the list of
    registered model names that :func:`attach_inference` returned, so
    ``detach_inference(fabric, attach_inference(fabric, inference))`` works.
    """
    if fabric is None:
        return False
    if isinstance(name, (list, tuple, set, frozenset)):
        wanted = {str(item) for item in name}
        provider_names = {str(item).split("/", 1)[0] for item in wanted}
    else:
        wanted = set()
        provider_names = {str(name or "inference")}
    removed = False
    for model in list(fabric.registry.list()):
        if model.name in wanted or model.provider in provider_names:
            try:
                fabric.registry.remove(model.name)
                removed = True
            except KeyError:
                continue
    providers = getattr(fabric, "providers", None)
    if providers is not None and hasattr(providers, "_providers"):
        for provider_name in provider_names:
            if provider_name in providers._providers:
                providers._providers.pop(provider_name, None)
                providers._info.pop(provider_name, None)
                removed = True
    return removed
