"""Bridge provider/model configuration into explicit runtime state.

Configuration is evidence that an operator supplied a usable configuration;
it is never evidence that a model is reachable or verified. Verification
remains a separate real runtime operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry
from forge.models.provider import ProviderInfo


@dataclass(frozen=True)
class _SnapshotModel:
    """Minimal model descriptor derived from a registry snapshot."""

    name: str
    capabilities: tuple[str, ...] = ()



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

    for provider_name in _provider_names(providers, models):
        info = _provider_info(providers, provider_name)
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


def _provider_names(providers: Any, models: Any) -> list[str]:
    """Return the provider names to synchronize, most reliable source first.

    A real :class:`~forge.models.provider.ProviderRegistry` exposes
    ``names()``. Minimal or third-party registries may not, in which case the
    providers actually referenced by registered models are used — the same
    set that could ever be routed to.
    """
    getter = getattr(providers, "names", None)
    if callable(getter):
        try:
            names = [str(name) for name in getter() or () if str(name)]
        except Exception:
            names = []
        if names:
            return sorted(set(names))
    discovered: set[str] = set()
    for item in _model_snapshot(models):
        provider = str(item.get("provider", "") or "")
        if provider:
            discovered.add(provider)
    return sorted(discovered)


def _provider_info(providers: Any, provider_name: str) -> Any:
    """Return provider metadata, or an empty stand-in when unavailable."""
    getter = getattr(providers, "info", None)
    if callable(getter):
        try:
            info = getter(provider_name)
            if info is not None:
                return info
        except Exception:
            pass
    return ProviderInfo(name=provider_name)


def _model_snapshot(model_registry: Any) -> list[dict]:
    """Return ``[{"name": ..., "provider": ...}, ...]`` from any registry."""
    snapshot = getattr(model_registry, "snapshot", None)
    if callable(snapshot):
        try:
            return [item for item in snapshot() or [] if isinstance(item, dict)]
        except Exception:
            return []
    # Fall back to a real ModelRegistry's own enumeration.
    names = getattr(model_registry, "names", None)
    if callable(names):
        result: list[dict] = []
        try:
            for name in names() or ():
                try:
                    model = model_registry.get(name)
                except Exception:
                    continue
                result.append({
                    "name": str(getattr(model, "name", "") or name),
                    "provider": str(getattr(model, "provider", "") or ""),
                })
        except Exception:
            return []
        return result
    return []


def _models_for_provider(model_registry: Any, provider_name: str) -> list[Any]:
    """Return the exact models a provider can serve.

    The registry snapshot is the authoritative list, so a registry that can
    only enumerate (no per-name ``get``) still synchronizes. Model objects
    are used when available because they carry capabilities; otherwise the
    snapshot's own fields are used.
    """
    getter = getattr(model_registry, "get", None)
    result: list[Any] = []
    seen: set[str] = set()
    for item in _model_snapshot(model_registry):
        if str(item.get("provider", "")) != provider_name:
            continue
        name = str(item.get("name", "") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        model = None
        if callable(getter):
            try:
                model = getter(name)
            except Exception:
                model = None
        if model is None:
            model = _SnapshotModel(
                name=name,
                capabilities=tuple(item.get("capabilities") or ()),
            )
        result.append(model)
    return sorted(result, key=lambda model: str(getattr(model, "name", "")))




def configured_runtime_snapshot(
    fabric: Any,
    registry: ConfiguredRuntimeRegistry = None,
) -> dict[str, Any]:
    """Return bounded, secret-free configured-runtime state."""
    return sync_configured_runtimes(fabric, registry).snapshot()
