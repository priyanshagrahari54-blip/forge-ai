"""Bridge provider/model configuration into explicit runtime state.

Configuration is evidence that an operator supplied a usable configuration;
it is never evidence that a model is reachable or verified. Verification
remains a separate real runtime operation.
"""
from __future__ import annotations

from typing import Any

from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry


def sync_configured_runtimes(
    fabric: Any,
    registry: ConfiguredRuntimeRegistry = None,
) -> ConfiguredRuntimeRegistry:
    """Synchronize configured provider/model pairs from a ModelFabric.

    Registration is only configuration evidence. No network call or health
    result is inferred, so this function can never promote a runtime to LIVE.
    """
    target = registry or ConfiguredRuntimeRegistry()
    providers = getattr(fabric, "providers", None)
    models = getattr(fabric, "registry", None)
    if providers is None or models is None:
        return target

    for provider_name in providers.names():
        info = providers.info(provider_name)
        for model in _models_for_provider(models, provider_name):
            model_id = str(getattr(model, "name", "") or "")
            if not model_id:
                continue
            runtime = target.maybe_get(provider_name, model_id)
            if runtime is None:
                runtime = ConfiguredRuntime(
                    provider=provider_name,
                    model_id=model_id,
                    endpoint=str(getattr(info, "endpoint", "") or ""),
                    capabilities=tuple(getattr(model, "capabilities", ()) or ()),
                    metadata={"local": bool(getattr(info, "local", False))},
                )
                target.register(runtime)
            if runtime.state in {"UNCONFIGURED", "CONFIGURED"}:
                runtime.set_configured(
                    valid=True, reason="provider and model are registered")
    return target


def _models_for_provider(model_registry: Any, provider_name: str) -> list[Any]:
    """Return exact models for a provider without relying on private fields."""
    try:
        snapshot = model_registry.snapshot()
    except Exception:
        return []
    names = sorted({
        str(item.get("name", ""))
        for item in snapshot
        if isinstance(item, dict)
        and str(item.get("provider", "")) == provider_name
        and item.get("name")
    })
    result = []
    for name in names:
        try:
            result.append(model_registry.get(name))
        except Exception:
            continue
    return result


def configured_runtime_snapshot(
    fabric: Any,
    registry: ConfiguredRuntimeRegistry = None,
) -> dict[str, Any]:
    """Return bounded, secret-free configured-runtime state."""
    return sync_configured_runtimes(fabric, registry).snapshot()
