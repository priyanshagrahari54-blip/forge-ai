"""Bridge from the Model Fabric to the Forge Native Model Runtime.

Direction of dependency, restated because it is the whole point::

    AI Engine (forge.core / forge.agents)
        -> Model Fabric (forge.models)   routing, policy, telemetry
            -> RuntimeProvider           this module
                -> ModelRuntime          forge.runtime.model_runtime
                    -> ModelBackend      native | ollama | llama.cpp | custom
                        -> model

``forge.runtime.model_runtime`` imports nothing from this package, so the
runtime stays independent of the AI Engine and can be embedded or shipped on
its own.  This module sits on the *engine* side of that boundary: it adapts a
:class:`~forge.runtime.model_runtime.ModelRuntime` to the
:class:`~forge.models.provider.ModelProvider` contract, so the AI Engine can
request inference through the runtime interface while the fabric keeps doing
what it already does — capability routing, policy filtering, failover, health
feedback, and telemetry.

Nothing here is wired up by default.  ``ModelFabric.from_defaults()`` is
unchanged; a runtime-backed provider is an explicit, opt-in registration, so
enabling the runtime can never silently change existing routing.
"""
from __future__ import annotations

from typing import Any, Iterator

from forge.models.capabilities import TEXT_CAPABILITIES
from forge.models.provider import ModelResult, ProviderInfo
from forge.models.registry import Model
from forge.runtime.model_runtime import (ModelRuntime, ModelRuntimeError,
                                         RuntimeRequest)

__all__ = ["RuntimeProvider", "attach_runtime"]


class RuntimeProvider:
    """Fabric provider whose inference goes through the model runtime.

    It is a thin adapter with no intelligence of its own: it translates the
    fabric's provider call into a :class:`RuntimeRequest`, hands it to the
    runtime, and translates the :class:`RuntimeResponse` back.  A runtime
    failure raises ``RuntimeError`` like every other provider in the fabric,
    so the existing failover chain and health feedback handle it unchanged.

    It never invents output: if the runtime reports that no backend could run
    inference, that failure is propagated verbatim.
    """

    def __init__(self, runtime: ModelRuntime, *, backend: str = "",
                 model: str = "", name: str = "runtime",
                 timeout: float | None = None) -> None:
        if runtime is None:
            raise ValueError("A ModelRuntime instance is required.")
        self.runtime = runtime
        self.backend = backend
        self.model = model
        self.name = name or "runtime"
        self.timeout = timeout

    # -- fabric provider contract ----------------------------------------

    def _request(self, prompt: str, *, context: str, task: str,
                 instructions: str, max_output_tokens: int | None,
                 temperature: float | None) -> RuntimeRequest:
        return RuntimeRequest(
            prompt=prompt or "",
            model=self.model,
            backend=self.backend,
            context=context or "",
            task=task or "",
            instructions=instructions or "",
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout=self.timeout,
        )

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        request = self._request(
            prompt, context=context, task=task, instructions=instructions,
            max_output_tokens=max_output_tokens, temperature=temperature)
        try:
            response = self.runtime.generate(request)
        except ModelRuntimeError as exc:
            raise RuntimeError(
                f"Model runtime rejected the request: {exc}") from exc
        if not response.success:
            raise RuntimeError(
                f"Runtime backend {response.backend or self.backend or '-'} "
                f"could not run inference ({response.error_kind or 'error'}): "
                f"{response.error}")
        return ModelResult(
            response.text,
            response.model or self.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency=(response.latency_ms or 0.0) / 1000.0,
        )

    def stream(self, prompt: str, *, context: str = "", task: str = "",
               instructions: str = "", max_output_tokens: int | None = None,
               temperature: float | None = None) -> Iterator[str]:
        """Yield text deltas from a runtime streaming generation."""
        request = self._request(
            prompt, context=context, task=task, instructions=instructions,
            max_output_tokens=max_output_tokens, temperature=temperature)
        try:
            stream = self.runtime.stream(request)
            for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except ModelRuntimeError as exc:
            # Acquiring the stream can fail too (closed runtime, unknown
            # backend, unknown model). Wrapping only the iteration would let
            # a ModelRuntimeError escape a provider whose contract is to
            # raise RuntimeError, which the fabric's failover understands.
            raise RuntimeError(
                f"Runtime streaming failed: {exc}") from exc

    def list_models(self) -> list[str]:
        """Runtime model names served by the selected backend (discovery)."""
        try:
            self.runtime.discover(self.backend)
            return [model.name
                    for model in self.runtime.models(self.backend)]
        except ModelRuntimeError:
            return []

    def health(self) -> dict[str, Any]:
        """Runtime health for the selected backend (or every backend)."""
        items = self.runtime.health(self.backend, probe=False)
        ready = [item.backend for item in items if item.ok]
        return {"available": bool(ready), "ready_backends": ready,
                "health": [item.to_dict() for item in items]}


def attach_runtime(fabric: Any, runtime: ModelRuntime, *, backend: str = "",
                   name: str = "runtime", register_models: bool = True,
                   capabilities: tuple[str, ...] = TEXT_CAPABILITIES,
                   context_window: int = 8192,
                   max_output_tokens: int = 2048,
                   model_prefix: str = "") -> list[str]:
    """Register a runtime-backed provider (and its models) with a fabric.

    Explicit and additive: the fabric's existing providers, models, and
    routing behaviour are untouched.  Returns the names of the fabric models
    that were registered.

    ``register_models`` runs runtime discovery for the selected backend and
    mirrors each discovered artifact into the fabric registry, so the router
    can select it like any other model.  Capabilities default to the
    conservative text set; the runtime never claims vision/tool support it
    has not verified.
    """
    if fabric is None:
        raise ValueError("A ModelFabric instance is required.")
    if runtime is None:
        raise ValueError("A ModelRuntime instance is required.")

    provider = RuntimeProvider(runtime, backend=backend, name=name)
    fabric.register_provider(
        name,
        provider,
        ProviderInfo(name=name, display_name="Forge Native Model Runtime",
                     kind="local", local=True, free=True,
                     capabilities=tuple(capabilities),
                     model=backend or ""),
    )
    if not register_models:
        return []

    prefix = model_prefix or name
    registered: list[str] = []
    try:
        runtime.discover(backend)
        found = runtime.models(backend)
    except ModelRuntimeError:
        return registered
    for entry in found:
        fabric_name = f"{prefix}/{entry.name}"
        if fabric.registry.has(fabric_name):
            continue
        try:
            fabric.register_model(Model(
                name=fabric_name,
                provider=name,
                capabilities=tuple(capabilities),
                context_window=int(entry.context_window or context_window),
                max_output_tokens=int(entry.max_output_tokens
                                      or max_output_tokens),
                free=True,
                local=bool(entry.local),
                metadata={
                    "runtime_backend": entry.backend,
                    "runtime_model_id": entry.model_id,
                    "runtime_format": entry.format,
                    "size_bytes": entry.size_bytes,
                    "description": ("Discovered by the Forge Native Model "
                                    "Runtime."),
                },
            ))
        except ValueError:
            continue
        registered.append(fabric_name)
    return registered
