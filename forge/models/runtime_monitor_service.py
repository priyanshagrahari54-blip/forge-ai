"""Background runtime verification loop for Forge Server.

The service turns configured provider/model pairs into explicit runtime state
(:class:`~forge.models.configured_runtime.RuntimeState`) using three kinds of
evidence, weakest first:

``discovered``
    The provider lists the exact model id (``list_models``). Inventory
    evidence only: it never makes a model routable and never revokes an
    earlier inference verdict. Inventories are collected once per provider
    per tick and published in the snapshot (``providers[<name>].discovered``)
    so an operator can see "124 discovered, 3 verified" instead of a claim.

``inference`` (probe)
    One bounded real generation (:func:`probe_provider_inference`). Runs at
    most once per configured runtime until it fails, then with exponential
    backoff, within a per-tick budget, so a paid provider is never charged on
    every tick. Only this evidence (or real traffic) promotes a runtime to
    LIVE and flips the fabric model to ``available``.

``inference`` (traffic)
    A successful production generation recorded by the fabric
    (:func:`record_inference_success`). The strongest evidence: the monitor
    promotes from it without spending a probe.

Conclusive negative evidence (id not listed, probe failed) makes the runtime
UNAVAILABLE and the model non-routable. Inconclusive probes (no list endpoint,
transport failure) leave the previous state untouched.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from forge.models.configured_runtime import (
    ConfiguredRuntime,
    ConfiguredRuntimeRegistry,
    RuntimeState,
)
from forge.models.configured_runtime_bridge import sync_configured_runtimes
from forge.models.runtime_monitor import RuntimeMonitor
from forge.models.runtime_verification import (
    DISCOVERED_STATUS,
    NOT_FOUND_STATUS,
    RuntimeProbeResult,
    apply_probe_result,
    is_inference_verified,
    probe_provider_inference,
)

log = logging.getLogger("forge.models.runtime_monitor")

#: Most inventory ids kept per provider in the persisted snapshot.
INVENTORY_LIMIT = 500
#: Environment switch for automatic inference probes (default on).
INFERENCE_PROBES_ENV = "FORGE_RUNTIME_INFERENCE_PROBES"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _candidate_ids(provider_name: str, model_id: str, provider: Any) -> Set[str]:
    """Every spelling under which a provider may list one registry model.

    Registry names are namespaced (``ollama/llama3.2``) while providers list
    bare ids (``llama3.2``); Ollama treats ``name`` and ``name:latest`` as the
    same tag; some adapters expose the served id as ``provider.model``.
    """
    names = {model_id}
    prefix = provider_name + "/"
    if model_id.startswith(prefix) and len(model_id) > len(prefix):
        names.add(model_id[len(prefix):])
    configured = str(getattr(provider, "model", "") or "")
    if configured and (configured == model_id or model_id.endswith("/" + configured)):
        names.add(configured)
    for name in list(names):
        if ":" not in name.rsplit("/", 1)[-1]:
            names.add(name + ":latest")
        elif name.endswith(":latest"):
            names.add(name[: -len(":latest")])
    return names


class RuntimeMonitorService:
    """Persistent runtime verification coordinator with live-registry feedback."""

    def __init__(self, fabric: Any, *, state_path: Any = ".forge/runtime-monitor.json",
                 interval_seconds: float = 60.0, verification_ttl_seconds: float = 300.0,
                 inference_probes: Optional[bool] = None, inference_probe_budget: int = 8,
                 inference_retry_seconds: float = 300.0,
                 inference_max_retry_seconds: float = 3600.0) -> None:
        self.fabric = fabric
        self.state_path = Path(state_path)
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.monitor = RuntimeMonitor(verification_ttl_seconds=verification_ttl_seconds)
        self.registry = ConfiguredRuntimeRegistry()
        self.inference_probes = (_env_flag(INFERENCE_PROBES_ENV, True)
                                 if inference_probes is None else bool(inference_probes))
        self.inference_probe_budget = max(0, int(inference_probe_budget))
        self.inference_retry_seconds = max(1.0, float(inference_retry_seconds))
        self.inference_max_retry_seconds = max(
            self.inference_retry_seconds, float(inference_max_retry_seconds))
        self._inventories: Dict[str, Dict[str, Any]] = {}
        self._last_tick = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="forge-runtime-monitor", daemon=True)
            self._thread.start()

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None and wait:
            thread.join(timeout=max(1.0, self.interval_seconds + 1.0))

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(force=True)
            except Exception:
                log.exception("runtime monitor tick failed")
            self._stop.wait(self.interval_seconds)

    # -- fabric access helpers ------------------------------------------------

    def _provider(self, provider_name: str) -> Any:
        providers = getattr(self.fabric, "providers", None)
        if providers is None:
            return None
        try:
            return providers.get(provider_name)
        except Exception:
            return None

    def _provider_kind(self, provider_name: str) -> str:
        providers = getattr(self.fabric, "providers", None)
        getter = getattr(providers, "info", None)
        if not callable(getter):
            return ""
        try:
            return str(getattr(getter(provider_name), "kind", "") or "")
        except Exception:
            return ""

    def _model(self, runtime: ConfiguredRuntime) -> Any:
        model_registry = getattr(self.fabric, "registry", None)
        if model_registry is None:
            return None
        try:
            model = model_registry.get(runtime.model_id)
        except Exception:
            return None
        if getattr(model, "provider", None) != runtime.provider:
            return None
        return model

    def _apply_to_registry(self, result: RuntimeProbeResult) -> None:
        model_registry = getattr(self.fabric, "registry", None)
        if model_registry is None:
            return
        # Registry feedback is a side effect: a registry that cannot resolve
        # the identity must not turn a probe into a reported outage.
        try:
            apply_probe_result(model_registry, result)
        except Exception:
            log.debug("registry feedback skipped for %s", result.model_id, exc_info=True)

    # -- discovery inventories ------------------------------------------------

    def _discover(self, provider_name: str, *, now: float) -> Optional[Set[str]]:
        """Collect one provider's inventory once per tick.

        Returns the set of listed ids, or ``None`` when the provider cannot be
        enumerated (no list endpoint, transport failure): that is absence of
        evidence, recorded in the snapshot but never treated as an outage.
        """
        provider = self._provider(provider_name)
        entry: Dict[str, Any] = {"checked": now, "discovered": [], "count": 0, "error": ""}
        if provider is None:
            entry["error"] = "provider is not registered"
            self._inventories[provider_name] = entry
            return None
        list_models = getattr(provider, "list_models", None)
        if not callable(list_models):
            entry["error"] = "provider has no model-list endpoint"
            entry["supports_discovery"] = False
            self._inventories[provider_name] = entry
            return None
        started = time.time()
        try:
            listed = list_models()
        except Exception as exc:
            entry["error"] = "discovery failed: %s" % type(exc).__name__
            entry["supports_discovery"] = True
            self._inventories[provider_name] = entry
            return None
        names: Set[str] = set()
        for item in listed or []:
            value = ((item.get("name") or item.get("id") or item.get("model"))
                     if isinstance(item, dict) else item)
            if value:
                names.add(str(value))
        entry.update({
            "supports_discovery": True,
            "latency_ms": round((time.time() - started) * 1000.0, 2),
            "count": len(names),
            "discovered": sorted(names)[:INVENTORY_LIMIT],
        })
        self._inventories[provider_name] = entry
        return names

    def _discovery_probe(self, runtime: ConfiguredRuntime,
                         inventory: Optional[Set[str]]) -> RuntimeProbeResult:
        provider = self._provider(runtime.provider)
        if provider is None:
            return RuntimeProbeResult(model_id=runtime.model_id, ok=False,
                                      status="unavailable", reason="provider is not registered")
        if inventory is None:
            error = str(self._inventories.get(runtime.provider, {}).get("error") or "")
            # No exact model-list evidence exists for this provider right now.
            # The probe is inconclusive (``conclusive=False``): Forge records
            # that it could not verify the runtime and leaves the model's
            # routing state untouched, rather than reporting an outage it
            # never observed.
            return RuntimeProbeResult(model_id=runtime.model_id, ok=False, status="unverified",
                                      reason=error or "provider has no exact model-list probe",
                                      conclusive=False)
        latency = float(self._inventories.get(runtime.provider, {}).get("latency_ms") or 0.0)
        if inventory.isdisjoint(_candidate_ids(runtime.provider, runtime.model_id, provider)):
            return RuntimeProbeResult(model_id=runtime.model_id, ok=False, latency_ms=latency,
                                      status=NOT_FOUND_STATUS,
                                      reason="exact model is not available")
        return RuntimeProbeResult(model_id=runtime.model_id, ok=True, latency_ms=latency,
                                  status=DISCOVERED_STATUS,
                                  reason="exact model listed by provider")

    # -- inference evidence -----------------------------------------------------

    def _promote(self, runtime: ConfiguredRuntime, *, verification_id: str,
                 capabilities: Iterable[str], checked_at: float) -> None:
        if runtime.state == RuntimeState.UNAVAILABLE.value:
            runtime.set_configured(valid=True)
        if runtime.state == RuntimeState.CONFIGURED.value:
            runtime.mark_verified(verification_id=verification_id,
                                  capabilities=capabilities, checked_at=checked_at)
            runtime.activate()
        elif runtime.state in {RuntimeState.VERIFIED.value, RuntimeState.LIVE.value}:
            runtime.state = RuntimeState.LIVE.value
            runtime.verification_id = verification_id
            runtime.last_checked = checked_at
        runtime.last_reason = ""
        runtime.metadata.pop("inference_failures", None)
        runtime.metadata.pop("next_inference_after", None)

    def _sync_traffic_evidence(self, runtime: ConfiguredRuntime, *, now: float) -> bool:
        """Promote from a real generation the fabric already recorded."""
        model = self._model(runtime)
        if model is None or not is_inference_verified(model):
            return False
        metadata = getattr(model, "metadata", {}) or {}
        verified_at = float(metadata.get("last_verified") or 0.0)
        source = str(metadata.get("verification_source") or "registry")
        if verified_at:
            if verified_at <= runtime.last_checked:
                return False  # older than the monitor's own verdict
        elif runtime.state in {RuntimeState.LIVE.value, RuntimeState.UNAVAILABLE.value}:
            # Undated evidence (a capability registered as verified at import)
            # counts once, never against a recorded negative verdict.
            return False
        checked_at = verified_at or now
        self._promote(runtime, verification_id="inference:%s:%d" % (source, int(checked_at)),
                      capabilities=tuple(getattr(model, "capabilities", ()) or ()),
                      checked_at=checked_at)
        return True

    def _needs_reverification(self, runtime: ConfiguredRuntime) -> bool:
        """A LIVE runtime must prove itself again in two situations.

        Its verdict came from a previous process (persisted state, nothing in
        this process has inferred with it yet), or its real traffic keeps
        failing (the fabric's health breaker tripped to ``unhealthy``).
        """
        if runtime.state != RuntimeState.LIVE.value:
            return False
        model = self._model(runtime)
        if model is None:
            return False
        if not is_inference_verified(model):
            return True
        health = getattr(model, "health", None)
        return str(getattr(health, "status", "")) == "unhealthy"

    def _inference_allowed(self, runtime: ConfiguredRuntime, *, now: float) -> bool:
        if not self.inference_probes:
            return False
        if runtime.state == RuntimeState.LIVE.value:
            if not self._needs_reverification(runtime):
                return False
        elif runtime.state not in {RuntimeState.CONFIGURED.value, RuntimeState.VERIFIED.value}:
            return False
        if float(runtime.metadata.get("next_inference_after") or 0.0) > now:
            return False
        if self._provider_kind(runtime.provider) == "fallback":
            return False
        model = self._model(runtime)
        if model is not None and bool(getattr(model, "fallback", False)):
            return False
        provider = self._provider(runtime.provider)
        return provider is not None and callable(getattr(provider, "generate", None))

    def _run_inference_probe(self, runtime: ConfiguredRuntime, *, now: float) -> Dict[str, Any]:
        provider = self._provider(runtime.provider)
        model = self._model(runtime)
        provider_model_id = getattr(model, "provider_model_id", None) if model is not None else None
        result = probe_provider_inference(provider, runtime.model_id,
                                          provider_model_id=provider_model_id)
        self._apply_to_registry(result)
        checked_at = float(now)
        runtime.last_checked = checked_at
        if result.ok:
            metadata = getattr(model, "metadata", None)
            if isinstance(metadata, dict):
                #: The monitor's clock is authoritative for its own probes so
                #: the next tick does not mistake this verdict for new traffic.
                metadata["last_verified"] = checked_at
                metadata["verification_source"] = "probe"
            self._promote(runtime, verification_id="inference:%s:%d" % (runtime.model_id, int(checked_at)),
                          capabilities=result.capabilities, checked_at=checked_at)
        else:
            failures = int(runtime.metadata.get("inference_failures") or 0) + 1
            delay = min(self.inference_max_retry_seconds,
                        self.inference_retry_seconds * (2 ** min(failures - 1, 10)))
            runtime.metadata["inference_failures"] = failures
            runtime.metadata["next_inference_after"] = checked_at + delay
            runtime.mark_unavailable(result.reason)
        return {"provider": runtime.provider, "model_id": runtime.model_id,
                "state": runtime.state, "probe": result.to_dict(),
                "verification_id": runtime.verification_id, "changed": True,
                "kind": "inference"}

    # -- the periodic tick ----------------------------------------------------------

    def tick(self, *, now: Optional[float] = None, force: bool = False) -> Dict[str, Any]:
        checked_at = float(time.time() if now is None else now)
        with self._lock:
            if not force and checked_at - self._last_tick < self.interval_seconds:
                return self.snapshot()
            self._last_tick = checked_at
            sync_configured_runtimes(self.fabric, self.registry)
            results: List[Dict[str, Any]] = []
            inventories: Dict[str, Optional[Set[str]]] = {}
            inference_budget = self.inference_probe_budget
            for runtime in self.registry.items():
                if runtime.state == RuntimeState.REVOKED.value:
                    continue
                if self._sync_traffic_evidence(runtime, now=checked_at):
                    results.append({"provider": runtime.provider, "model_id": runtime.model_id,
                                    "state": runtime.state, "changed": True,
                                    "verification_id": runtime.verification_id,
                                    "kind": "traffic"})
                if (runtime.state == RuntimeState.LIVE.value
                        and not self.monitor.stale(runtime, now=checked_at)
                        and not self._needs_reverification(runtime)):
                    continue
                if runtime.provider not in inventories:
                    inventories[runtime.provider] = self._discover(runtime.provider, now=checked_at)
                inventory = inventories[runtime.provider]
                try:
                    result = self.monitor.check(
                        runtime, lambda _p, _m: self._discovery_probe(runtime, inventory),
                        now=checked_at)
                except Exception as exc:
                    runtime.mark_unavailable("runtime probe failed: %s" % type(exc).__name__)
                    self._apply_to_registry(RuntimeProbeResult(
                        model_id=runtime.model_id, ok=False, status="unhealthy",
                        reason=runtime.last_reason))
                    results.append({"provider": runtime.provider, "model_id": runtime.model_id,
                                    "state": runtime.state, "changed": True,
                                    "error": type(exc).__name__})
                    continue
                if result.probe.conclusive:
                    self._apply_to_registry(result.probe)
                results.append(result.to_dict())
                if result.probe.conclusive and not result.probe.ok:
                    continue  # conclusive negative: nothing to verify
                if inference_budget > 0 and self._inference_allowed(runtime, now=checked_at):
                    inference_budget -= 1
                    results.append(self._run_inference_probe(runtime, now=checked_at))
            self._save()
            return {"time": checked_at, "results": results, **self.snapshot()}

    # -- explicit operator checks ------------------------------------------------

    def inference_check(self, provider_name: str, model_id: str) -> Dict[str, Any]:
        """Run one explicit real inference probe for an exact configured runtime."""
        with self._lock:
            sync_configured_runtimes(self.fabric, self.registry)
            runtime = self.registry.maybe_get(provider_name, model_id)
            if runtime is None:
                raise KeyError("configured runtime not found: %s:%s" % (provider_name, model_id))
            provider = self._provider(provider_name)
            if provider is None:
                runtime.mark_unavailable("provider is not registered")
                self._save()
                return {"provider": provider_name, "model_id": model_id, "ok": False,
                        "state": runtime.state, "reason": runtime.last_reason}
            outcome = self._run_inference_probe(runtime, now=time.time())
            self._save()
            return {"provider": provider_name, "model_id": model_id,
                    "ok": bool(outcome["probe"]["ok"]), "state": runtime.state,
                    "probe": outcome["probe"]}

    # -- reporting ---------------------------------------------------------------------

    def provider_summary(self) -> Dict[str, Dict[str, Any]]:
        """Per-provider counts in the precise state vocabulary.

        ``configured`` counts registered runtimes; ``discovered`` is the size
        of the provider's last inventory (evidence, not routability);
        ``verified``/``live`` count runtimes with an inference verdict.
        """
        summary: Dict[str, Dict[str, Any]] = {}
        for runtime in self.registry.items():
            entry = summary.setdefault(runtime.provider, {
                "configured": 0, "live": 0, "verified": 0, "unavailable": 0,
                "discovered": None, "discovery_error": "", "last_discovery": 0.0,
                "discovered_models": [],
            })
            entry["configured"] += 1
            if runtime.state == RuntimeState.LIVE.value:
                entry["live"] += 1
                entry["verified"] += 1
            elif runtime.state == RuntimeState.VERIFIED.value:
                entry["verified"] += 1
            elif runtime.state == RuntimeState.UNAVAILABLE.value:
                entry["unavailable"] += 1
        for provider_name, inventory in self._inventories.items():
            entry = summary.setdefault(provider_name, {
                "configured": 0, "live": 0, "verified": 0, "unavailable": 0,
                "discovered": None, "discovery_error": "", "last_discovery": 0.0,
                "discovered_models": [],
            })
            if inventory.get("supports_discovery") and not inventory.get("error"):
                entry["discovered"] = int(inventory.get("count") or 0)
                entry["discovered_models"] = list(inventory.get("discovered") or [])
            entry["discovery_error"] = str(inventory.get("error") or "")
            entry["last_discovery"] = float(inventory.get("checked") or 0.0)
        return summary

    def snapshot(self) -> Dict[str, Any]:
        counts = self.registry.counts()
        return {
            "schema_version": 2,
            "running": self.running,
            "last_tick": self._last_tick,
            "interval_seconds": self.interval_seconds,
            "verification_ttl_seconds": self.monitor.verification_ttl_seconds,
            "inference_probes": self.inference_probes,
            "counts": counts,
            "live": counts.get(RuntimeState.LIVE.value, 0),
            "runtimes": self.registry.snapshot(),
            "providers": self.provider_summary(),
        }

    # -- persistence ---------------------------------------------------------------

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        payload["runtime_metadata"] = {
            ConfiguredRuntimeRegistry.key(runtime.provider, runtime.model_id): {
                key: value for key, value in runtime.metadata.items()
                if key in ("inference_failures", "next_inference_after", "local")
            }
            for runtime in self.registry.items()
        }
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(str(temp), str(self.state_path))

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        saved_metadata = payload.get("runtime_metadata") or {}
        for item in payload.get("runtimes") or []:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            model_id = str(item.get("model_id") or "").strip()
            if not provider:
                continue
            metadata = saved_metadata.get(ConfiguredRuntimeRegistry.key(provider, model_id))
            runtime = ConfiguredRuntime(
                provider=provider, model_id=model_id,
                endpoint=str(item.get("endpoint") or ""),
                state=str(item.get("state") or RuntimeState.UNCONFIGURED.value),
                capabilities=tuple(item.get("capabilities") or ()),
                verification_id=str(item.get("verification_id") or ""),
                last_reason=str(item.get("last_reason") or "")[:500],
                last_checked=float(item.get("last_checked") or 0.0),
                metadata=dict(metadata) if isinstance(metadata, dict) else {},
            )
            if self.registry.maybe_get(provider, model_id) is None:
                self.registry.register(runtime)
        for provider_name, inventory in (payload.get("providers") or {}).items():
            if isinstance(inventory, dict) and inventory.get("discovered") is not None:
                self._inventories[str(provider_name)] = {
                    "checked": float(inventory.get("last_discovery") or 0.0),
                    "count": int(inventory.get("discovered") or 0),
                    "discovered": list(inventory.get("discovered_models") or []),
                    "error": str(inventory.get("discovery_error") or ""),
                    "supports_discovery": True,
                }
        self._last_tick = float(payload.get("last_tick") or 0.0)
