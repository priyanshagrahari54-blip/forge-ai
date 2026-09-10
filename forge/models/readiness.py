"""Model readiness probing: why would a task fail before it even starts?

Every autonomous task needs a *real* code-generating model. Out of the box
Forge registers two candidates:

* ``ollama/<model>`` — a real local model, but only if the Ollama server is
  running **and** the configured model has been pulled.
* ``local-fallback`` — a deterministic placeholder that honestly refuses to
  synthesize code (it returns ``{"changes": {}}``).

When Ollama is unreachable and no remote provider key is configured, routing
silently falls through to the placeholder, the coder sees zero changes, and
the task fails with the cryptic ``"Model proposed no changes"``. This module
makes that failure explainable and actionable:

* :func:`check_fabric_readiness` actively probes the fabric (cheap, bounded,
  no secrets leaked) and reports exactly which link is broken.
* :func:`describe_no_model_error` turns a report into a human-actionable
  error message used by the coder, the supervisor pre-flight gate, the
  ``forge doctor`` CLI, and the cockpit/desktop readiness views.
* :func:`fabric_has_real_model` answers the fast static question "is any
  non-fallback model even registered?" without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReadinessCheck:
    """One named probe result."""

    name: str
    ok: bool
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass
class ReadinessReport:
    """Aggregated readiness of a ModelFabric for coding tasks."""

    ready: bool
    checks: list[ReadinessCheck] = field(default_factory=list)
    usable_models: list[str] = field(default_factory=list)
    fallback_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "fallback_only": self.fallback_only,
            "usable_models": list(self.usable_models),
            "checks": [check.to_dict() for check in self.checks],
        }

    @property
    def failures(self) -> list[ReadinessCheck]:
        return [check for check in self.checks if not check.ok]

    @property
    def remediations(self) -> list[str]:
        seen: list[str] = []
        for check in self.failures:
            if check.remediation and check.remediation not in seen:
                seen.append(check.remediation)
        return seen


def fabric_has_real_model(fabric: Any) -> bool:
    """Return True when any registered, non-fallback model has a provider.

    Static only: no network access. A fabric where this returns False can
    never produce code, so callers should fail fast with
    :func:`describe_no_model_error` instead of running a doomed task.
    """
    try:
        registry = getattr(fabric, "registry", None)
        providers = getattr(fabric, "providers", None)
        models = registry.list() if registry is not None else []
    except Exception:
        return False
    for model in models or []:
        try:
            if bool(getattr(model, "fallback", False)):
                continue
            if not bool(getattr(model, "available", True)):
                continue
            provider_name = str(getattr(model, "provider", "") or "")
            if providers is not None and not providers.has(provider_name):
                continue
            return True
        except Exception:
            continue
    return False


def is_fallback_response(fabric: Any, model_name: str, provider_name: str) -> bool:
    """Return True when a response came from the deterministic placeholder."""
    if (provider_name or "").strip().lower() == "local":
        return True
    try:
        model = fabric.registry.get(model_name)
    except Exception:
        return False
    return bool(getattr(model, "fallback", False))


def check_fabric_readiness(fabric: Any, *, probe_network: bool = True,
                           timeout: float = 5.0) -> ReadinessReport:
    """Probe a ModelFabric and report whether coding tasks can succeed.

    ``probe_network=False`` skips live endpoint checks (pure registry/config
    inspection). Network probes use short timeouts and never raise: an
    unreachable endpoint is reported as a failed check, not an exception.
    Secret values are never read or reported — only whether a credential is
    configured.
    """
    checks: list[ReadinessCheck] = []
    usable: list[str] = []

    # 1. Static: is any real (non-fallback) model registered?
    try:
        models = list(getattr(fabric, "registry", None).list() or [])
    except Exception:
        models = []
    real_models = [m for m in models if not bool(getattr(m, "fallback", False))]
    fallback_only = bool(models) and not real_models
    if not real_models:
        checks.append(ReadinessCheck(
            name="real_model_registered",
            ok=False,
            detail="No non-fallback model is registered; only the offline placeholder exists.",
            remediation=("Configure a model provider: install Ollama "
                         "(https://ollama.com) and pull a model, e.g. "
                         "`ollama pull llama3.2`, or set OPENAI_API_KEY to "
                         "enable the optional OpenAI provider."),
        ))
    else:
        names = sorted(str(getattr(m, "name", "?")) for m in real_models)
        checks.append(ReadinessCheck(
            name="real_model_registered",
            ok=True,
            detail=f"Registered real models: {', '.join(names)}.",
        ))

    providers = getattr(fabric, "providers", None)
    provider_names: set[str] = set()
    try:
        provider_names = set(providers.names()) if providers is not None else set()
    except Exception:
        provider_names = set()

    # 2. Ollama: reachable + model pulled?
    ollama_models = [m for m in real_models
                     if str(getattr(m, "provider", "")) == "ollama"]
    if ollama_models and "ollama" in provider_names and providers is not None:
        try:
            provider = providers.get("ollama")
        except Exception as exc:
            checks.append(ReadinessCheck(
                name="ollama",
                ok=False,
                detail=f"Ollama provider lookup failed: {exc}.",
                remediation="Rebuild the fabric with `ModelFabric.from_defaults()`.",
            ))
            provider = None
        if provider is not None:
            endpoint = ""
            try:
                endpoint = str(getattr(provider, "url", "") or "")
            except Exception:
                endpoint = ""
            wanted = ""
            try:
                wanted = str(getattr(provider, "model", "") or "")
            except Exception:
                wanted = ""
            if not probe_network:
                checks.append(ReadinessCheck(
                    name="ollama",
                    ok=True,
                    detail=(f"Ollama provider registered (model {wanted!r}); "
                            f"live probe skipped."),
                ))
                usable.extend(str(getattr(m, "name", "?")) for m in ollama_models)
            else:
                served: list[str] | None = None
                probe_error = ""
                try:
                    original_timeout = getattr(provider, "timeout", None)
                    try:
                        provider.timeout = min(float(original_timeout or timeout), timeout)
                    except Exception:
                        pass
                    served = provider.list_models()
                except Exception as exc:
                    probe_error = str(exc)
                finally:
                    try:
                        if original_timeout is not None:
                            provider.timeout = original_timeout
                    except Exception:
                        pass
                if served is None:
                    checks.append(ReadinessCheck(
                        name="ollama",
                        ok=False,
                        detail=(f"Ollama server not reachable at {endpoint or 'the configured URL'} "
                                f"({probe_error or 'connection failed'})."),
                        remediation=("Start Ollama (`ollama serve`) and pull the model "
                                     f"(`ollama pull {wanted or 'llama3.2'}`), or point "
                                     "OLLAMA_URL / OLLAMA_MODEL at a running server."),
                    ))
                else:
                    pulled = any(
                        name == wanted or name.split(":")[0] == (wanted or "").split(":")[0]
                        for name in served
                    )
                    if pulled:
                        checks.append(ReadinessCheck(
                            name="ollama",
                            ok=True,
                            detail=(f"Ollama reachable at {endpoint}; model {wanted!r} "
                                    f"is available (served: {', '.join(served) or 'none listed'})."),
                        ))
                        usable.extend(str(getattr(m, "name", "?")) for m in ollama_models)
                    else:
                        checks.append(ReadinessCheck(
                            name="ollama",
                            ok=False,
                            detail=(f"Ollama reachable at {endpoint} but model {wanted!r} "
                                    f"is not pulled (served: {', '.join(served) or 'none'})."),
                            remediation=f"Run `ollama pull {wanted or 'llama3.2'}` and retry the task.",
                        ))

    # 3. OpenAI: configured?
    openai_models = [m for m in real_models
                     if str(getattr(m, "provider", "")) == "openai"]
    credentials = getattr(fabric, "credentials", None)
    openai_configured = False
    if credentials is not None:
        try:
            openai_configured = bool(credentials.configured("openai"))
        except Exception:
            openai_configured = False
    if openai_models:
        if openai_configured:
            checks.append(ReadinessCheck(
                name="openai",
                ok=True,
                detail="OpenAI provider registered with a configured credential.",
            ))
            usable.extend(str(getattr(m, "name", "?")) for m in openai_models)
        else:
            checks.append(ReadinessCheck(
                name="openai",
                ok=False,
                detail="OpenAI provider registered but no credential is configured.",
                remediation="Set the OPENAI_API_KEY environment variable (never commit it).",
            ))
    elif openai_configured:
        checks.append(ReadinessCheck(
            name="openai",
            ok=True,
            detail="OpenAI credential is configured (provider enables itself on next fabric build).",
        ))

    # 4. Other/unknown providers with real models: count them usable when the
    # provider object exists (custom providers, mocks in tests).
    for model in real_models:
        provider_name = str(getattr(model, "provider", "") or "")
        if provider_name in ("ollama", "openai"):
            continue
        try:
            exists = providers.has(provider_name) if providers is not None else False
        except Exception:
            exists = False
        label = str(getattr(model, "name", "?"))
        if exists:
            usable.append(label)
            checks.append(ReadinessCheck(
                name=f"provider:{provider_name or 'unknown'}",
                ok=True,
                detail=f"Custom provider {provider_name!r} registered for model {label!r}.",
            ))
        else:
            checks.append(ReadinessCheck(
                name=f"provider:{provider_name or 'unknown'}",
                ok=False,
                detail=(f"Model {label!r} has no registered provider "
                        f"{provider_name!r}; it can never be called."),
                remediation=(f"Register provider {provider_name!r} before model "
                             f"{label!r}, or remove the model."),
            ))

    usable = sorted(set(usable))
    ready = bool(usable)
    return ReadinessReport(ready=ready, checks=checks,
                           usable_models=usable,
                           fallback_only=fallback_only or not ready)


def describe_no_model_error(report: ReadinessReport | None = None,
                            fabric: Any = None) -> str:
    """Build the actionable error shown when no code model can serve a task.

    Pass an existing report to reuse its probes, or a fabric to probe
    statically (``probe_network=False`` keeps this fast and side-effect
    free; live Ollama state is described generically instead).
    """
    if report is None:
        if fabric is not None:
            report = check_fabric_readiness(fabric, probe_network=False)
        else:
            report = ReadinessReport(ready=False, fallback_only=True)
    lines = [
        "No working code model is available, so this task cannot produce code.",
        ("The request fell through to Forge's offline placeholder, which "
         "honestly refuses to invent source code instead of faking it."),
    ]
    for check in report.failures:
        if check.detail:
            lines.append(f"- {check.name}: {check.detail}")
    remediations = report.remediations
    if not remediations:
        remediations = [
            ("Install Ollama (https://ollama.com), run `ollama serve`, then "
             "`ollama pull llama3.2`."),
            "Or set OPENAI_API_KEY to enable the optional OpenAI provider.",
        ]
    lines.append("To fix:")
    for index, remediation in enumerate(remediations, 1):
        lines.append(f"  {index}. {remediation}")
    lines.append("Run `forge doctor` for a full diagnosis.")
    return "\n".join(lines)
