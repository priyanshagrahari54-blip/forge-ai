"""The canonical model registry (Session 11).

One registry, one truth. Every usable model in Forge is a
:class:`~forge.models.identity.ModelIdentity` held here, together with its
backend, its capabilities, its resource requirements, its verification status,
its artifact fingerprint, its configuration, its availability, and its last
health result.

Operations: ``register``, ``discover``, ``verify``, ``load``, ``unload``,
``status``, ``remove``.

* ``discover`` asks real backends what they can actually see. A model that
  cannot be discovered is not registered as available.
* ``verify`` runs :class:`~forge.models.verification.ModelVerifier` — a real
  fingerprint, a real health probe and a real generation. Configuration never
  produces READY.
* ``load`` goes through the resource governor *and* the bounded residency
  cache, so two agents asking for the same model produce exactly one load.
* ``remove`` is safe: an entry with live references is deferred, never torn
  down under an active request.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, Optional, Sequence, Tuple)

from forge.models.backends import (Backend, BackendError, BackendRegistry,
                                   BackendStatus)
from forge.models.identity import (AvailabilityState, IdentityError,
                                   ModelIdentity, VerificationState)
from forge.models.model_cache import (ModelResidencyCache,
                                      ModelResidencyError)
from forge.models.verification import ModelVerifier, VerificationResult

__all__ = ["CatalogError", "DiscoveryReport", "ModelCatalog"]


class CatalogError(RuntimeError):
    """A registry operation was refused."""

    code = "CATALOG"

    def __init__(self, message: str, *, code: str = "CATALOG") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class DiscoveryReport:
    """What discovery actually found (honest, per backend)."""

    identities: List[ModelIdentity] = field(default_factory=list)
    per_backend: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    registered: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    duration_ms: float = 0.0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": len(self.identities),
            "registered": list(self.registered),
            "unchanged": list(self.unchanged),
            "per_backend": {key: dict(value)
                            for key, value in self.per_backend.items()},
            "errors": [dict(item) for item in self.errors],
            "duration_ms": round(float(self.duration_ms or 0.0), 3),
            "at": self.at,
        }


class ModelCatalog:
    """The one canonical registry of model identities."""

    def __init__(self, *, backends: Optional[BackendRegistry] = None,
                 cache: Optional[ModelResidencyCache] = None,
                 verifier: Optional[ModelVerifier] = None,
                 governor: Any = None, telemetry: Any = None,
                 max_resident_bytes: int = 0, max_slots: int = 2,
                 idle_seconds: float = 0.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backends = backends if backends is not None else BackendRegistry()
        self.governor = governor
        self.telemetry = telemetry
        self.verifier = verifier if verifier is not None else ModelVerifier()
        self.cache = cache if cache is not None else ModelResidencyCache(
            max_bytes=max_resident_bytes, max_slots=max_slots,
            idle_seconds=idle_seconds, unloader=self._unload_via_backend,
            clock=clock)
        if self.cache._unloader is None:
            self.cache._unloader = self._unload_via_backend
        self._identities: Dict[str, ModelIdentity] = {}
        self._lock = threading.RLock()
        self._denials: List[Dict[str, Any]] = []

    # -- registration ----------------------------------------------------

    def register(self, identity: ModelIdentity, *,
                 replace: bool = False) -> ModelIdentity:
        if identity is None:
            raise CatalogError("a ModelIdentity is required",
                               code="BAD_REQUEST")
        if not isinstance(identity, ModelIdentity):
            raise CatalogError(
                "expected a ModelIdentity, got %s"
                % type(identity).__name__, code="BAD_REQUEST")
        with self._lock:
            if identity.model_id in self._identities and not replace:
                raise CatalogError(
                    "model already registered: %s" % identity.model_id,
                    code="DUPLICATE")
            self._identities[identity.model_id] = identity
        self._emit("register", model_id=identity.model_id,
                   backend_id=identity.backend_id,
                   availability_state=identity.availability_state)
        return identity

    def register_backend(self, backend: Backend, *,
                         replace: bool = False) -> Backend:
        return self.backends.register(backend, replace=replace)

    def get(self, model_id: str) -> ModelIdentity:
        with self._lock:
            try:
                return self._identities[model_id]
            except KeyError:
                bare = str(model_id or "").split(":", 1)[-1]
                for key, value in self._identities.items():
                    if value.name == bare:
                        return value
                raise CatalogError(
                    "unknown model %r (registered: %s)"
                    % (model_id, ", ".join(sorted(self._identities)) or "-"),
                    code="NOT_FOUND") from None

    def find(self, model_id: str) -> Optional[ModelIdentity]:
        try:
            return self.get(model_id)
        except CatalogError:
            return None

    def has(self, model_id: str) -> bool:
        with self._lock:
            return model_id in self._identities

    def list(self, *, capability: str = "", backend_id: str = "",
             state: str = "", usable_only: bool = False,
             local_only: Optional[bool] = None) -> List[ModelIdentity]:
        with self._lock:
            values = [self._identities[key]
                      for key in sorted(self._identities)]
        if capability:
            values = [item for item in values if item.supports(capability)]
        if backend_id:
            values = [item for item in values if item.backend_id == backend_id]
        if state:
            values = [item for item in values
                      if item.availability_state == state]
        if usable_only:
            values = [item for item in values if item.usable]
        if local_only is not None:
            values = [item for item in values
                      if bool(item.local) == bool(local_only)]
        return values

    def identities(self) -> List[ModelIdentity]:
        return self.list()

    def model_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._identities)

    def __len__(self) -> int:
        with self._lock:
            return len(self._identities)

    def __contains__(self, model_id: object) -> bool:
        return isinstance(model_id, str) and self.has(model_id)

    # -- discovery -------------------------------------------------------

    def discover(self, *, backend_id: str = "",
                 register: bool = True) -> DiscoveryReport:
        """Ask real backends what they can see; register what they report."""
        started = time.perf_counter()
        report = DiscoveryReport()
        targets = ([self.backends.get(backend_id)] if backend_id
                   else self.backends.list())
        for backend in targets:
            entry: Dict[str, Any] = {"backend_id": backend.backend_id,
                                     "found": 0, "error": ""}
            try:
                found = backend.discover()
            except BackendError as exc:
                entry["error"] = "%s: %s" % (exc.code, exc.message)
                report.errors.append({"backend_id": backend.backend_id,
                                      "error": entry["error"]})
                report.per_backend[backend.backend_id] = entry
                continue
            except Exception as exc:
                entry["error"] = str(exc)[:300]
                report.errors.append({"backend_id": backend.backend_id,
                                      "error": entry["error"]})
                report.per_backend[backend.backend_id] = entry
                continue
            entry["found"] = len(found)
            report.per_backend[backend.backend_id] = entry
            for identity in found:
                report.identities.append(identity)
                if not register:
                    continue
                with self._lock:
                    existing = self._identities.get(identity.model_id)
                if existing is None:
                    self.register(identity)
                    report.registered.append(identity.model_id)
                else:
                    self._refresh(existing, identity, backend)
                    report.unchanged.append(identity.model_id)
        # A model that disappeared from discovery is no longer available.
        for identity in self.list():
            if backend_id and identity.backend_id != backend_id:
                continue
            if any(item.model_id == identity.model_id
                   for item in report.identities):
                continue
            if identity.availability_state in (
                    AvailabilityState.READY.value,
                    AvailabilityState.LOADED.value,
                    AvailabilityState.CONFIGURED.value,
                    AvailabilityState.DISCOVERED.value):
                identity.set_availability(AvailabilityState.UNAVAILABLE.value,
                                          reason="no longer discovered",
                                          strict=False)
        report.duration_ms = (time.perf_counter() - started) * 1000.0
        self._emit("discover", found=len(report.identities),
                   registered=len(report.registered),
                   errors=len(report.errors))
        return report

    def _refresh(self, existing: ModelIdentity, discovered: ModelIdentity,
                 backend: Backend) -> None:
        """Merge a fresh discovery into a registered identity.

        Verification is *not* carried over blindly: a changed fingerprint
        invalidates it (cache poisoning / artifact swap protection).
        """
        if discovered.artifact_fingerprint and existing.artifact_fingerprint \
                and discovered.artifact_fingerprint != existing.artifact_fingerprint:
            existing.set_verification(
                VerificationState.EXPIRED.value,
                detail="artifact fingerprint changed since verification")
            existing.set_availability(AvailabilityState.UNVERIFIED.value,
                                      reason="artifact changed", strict=False)
        existing.artifact_fingerprint = (discovered.artifact_fingerprint
                                         or existing.artifact_fingerprint)
        existing.artifact_path = discovered.artifact_path or existing.artifact_path
        existing.size_bytes = discovered.size_bytes or existing.size_bytes
        existing.context_limit = (discovered.context_limit
                                  or existing.context_limit)
        existing.model_family = discovered.model_family or existing.model_family
        existing.quantization = discovered.quantization or existing.quantization
        existing.parameter_count = (discovered.parameter_count
                                    if discovered.parameter_count is not None
                                    else existing.parameter_count)
        existing.memory_requirements = (discovered.memory_requirements
                                        if discovered.memory_requirements.measured
                                        else existing.memory_requirements)
        for key, value in (discovered.metadata or {}).items():
            existing.metadata.setdefault(key, value)
        existing.updated_at = time.time()
        if discovered.availability_state == AvailabilityState.LOADED.value \
                and existing.availability_state not in (
                    AvailabilityState.READY.value,
                    AvailabilityState.LOADED.value):
            existing.set_availability(AvailabilityState.LOADED.value,
                                      reason="backend reports loaded",
                                      strict=False)

    # -- verification ----------------------------------------------------

    def verify(self, model_id: str, *, backend_id: str = "",
               expected_fingerprint: str = "") -> VerificationResult:
        """Verify one model with a real check. Never raises."""
        try:
            identity = self.get(model_id)
        except CatalogError as exc:
            return VerificationResult(
                model_id=model_id, verified=False,
                state=VerificationState.FAILED.value,
                error=exc.message, error_code=exc.code)
        backend = self._backend_for(identity, backend_id)
        if backend is None:
            result = VerificationResult(
                model_id=identity.model_id, verified=False,
                state=VerificationState.FAILED.value,
                error="no backend serves %s" % identity.model_id,
                error_code="BACKEND_NOT_FOUND")
            identity.set_verification(VerificationState.FAILED.value,
                                      detail=result.error)
            return result
        result = self.verifier.verify(
            identity, backend, expected_fingerprint=expected_fingerprint)
        mark = getattr(backend, "mark_verified", None)
        if callable(mark):
            if result.verified:
                mark(identity.model_id,
                     ttl_seconds=self.verifier.ttl_seconds)
            else:
                unmark = getattr(backend, "mark_unverified", None)
                if callable(unmark):
                    unmark(identity.model_id)
        self._emit("verify", model_id=identity.model_id,
                   backend_id=backend.backend_id, verified=result.verified,
                   error_code=result.error_code,
                   latency_ms=result.latency_ms)
        return result

    def verify_all(self, *, backend_id: str = "",
                   usable_only: bool = False) -> List[VerificationResult]:
        targets = self.list(backend_id=backend_id,
                            usable_only=usable_only)
        return [self.verify(identity.model_id) for identity in targets]

    # -- load / unload ---------------------------------------------------

    def load(self, model_id: str, *, backend_id: str = "",
             timeout: Optional[float] = None,
             size_bytes: Optional[int] = None) -> Dict[str, Any]:
        """Load a model: governor first, then single-flight residency.

        Returns a record with the identity, the residency entry and the
        resource decision. Raises :class:`CatalogError` on refusal — a
        refusal is never a silent no-op.
        """
        identity = self.get(model_id)
        backend = self._backend_for(identity, backend_id)
        if backend is None:
            raise CatalogError("no backend serves %s" % identity.model_id,
                               code="BACKEND_NOT_FOUND")

        # Governor: refuse before touching the backend (spec §10).
        size = int(size_bytes if size_bytes is not None
                   else (identity.memory_requirements.resident_bytes
                         or identity.size_bytes or 0))
        decision = self._governor_load_check(identity, size)
        if not decision["allowed"]:
            identity.set_availability(AvailabilityState.RESOURCE_DENIED.value,
                                      reason=decision["reason"], strict=False)
            self._deny("MODEL_LOAD", decision["reason"],
                       model_id=identity.model_id)
            raise CatalogError(decision["reason"],
                               code=decision.get("code", "RESOURCE_DENIED"))

        status = backend.health(probe=False)
        if getattr(status, "denial", ""):
            identity.set_availability(AvailabilityState.POLICY_DENIED.value,
                                      reason=status.denial, strict=False)
            self._deny("NETWORK_POLICY", status.denial,
                       model_id=identity.model_id)
            raise CatalogError(status.denial, code="NETWORK_POLICY")

        started = time.time()
        try:
            entry = self.cache.acquire(
                identity.model_id,
                lambda: backend.load(identity.model_id),
                backend_id=backend.backend_id, size_bytes=size,
                fingerprint=identity.artifact_fingerprint, timeout=timeout)
        except ModelResidencyError as exc:
            identity.set_availability(AvailabilityState.RESOURCE_DENIED.value,
                                      reason=exc.message, strict=False)
            self._deny(exc.code, exc.message, model_id=identity.model_id)
            raise CatalogError(exc.message, code=exc.code) from exc
        except BackendError as exc:
            identity.set_availability(AvailabilityState.FAILED.value,
                                      reason=exc.message, strict=False)
            raise CatalogError(exc.message, code=exc.code) from exc
        except Exception as exc:
            identity.set_availability(AvailabilityState.FAILED.value,
                                      reason=str(exc)[:300], strict=False)
            raise CatalogError("load failed: %s" % str(exc)[:300],
                               code="LOAD_FAILED") from exc

        try:
            self.cache.release(identity.model_id)
        except Exception:
            pass
        identity.set_availability(AvailabilityState.LOADED.value,
                                  reason="resident", strict=False)
        loaded = {
            "model_id": identity.model_id,
            "backend_id": backend.backend_id,
            "loaded": True,
            "duration_ms": round((time.time() - started) * 1000.0, 3),
            "size_bytes": int(entry.size_bytes or 0),
            "residency": self.cache.stats(),
            "resource_decision": decision,
            "identity": identity.to_dict(),
        }
        self._emit("load", model_id=identity.model_id,
                   backend_id=backend.backend_id, size_bytes=entry.size_bytes)
        return loaded

    def unload(self, model_id: str, *, force: bool = False) -> Dict[str, Any]:
        """Release residency. Never tears down a model an request is using."""
        identity = self.find(model_id)
        key = identity.model_id if identity is not None else model_id
        entry = self.cache.get(key)
        if entry is None:
            return {"unloaded": False, "model_id": key,
                    "reason": "not resident"}
        if entry.refs > 0 and not force:
            return {"unloaded": False, "deferred": True, "model_id": key,
                    "refs": entry.refs,
                    "reason": ("in use by %d active reference(s); unload "
                               "deferred" % entry.refs)}
        removed = self.cache.remove(key)
        if identity is not None and removed.get("removed"):
            identity.set_availability(AvailabilityState.CONFIGURED.value,
                                      reason="unloaded", strict=False)
        self._emit("unload", model_id=key, removed=bool(removed.get("removed")))
        return {"unloaded": bool(removed.get("removed")),
                "deferred": bool(removed.get("deferred")),
                "model_id": key, "detail": removed}

    def residency(self, model_id: str, *, timeout: Optional[float] = None):
        """Context manager: hold a loaded model for the duration of a call."""
        identity = self.get(model_id)
        backend = self._backend_for(identity, "")
        if backend is None:
            raise CatalogError("no backend serves %s" % identity.model_id,
                               code="BACKEND_NOT_FOUND")
        decision = self._governor_load_check(
            identity, int(identity.memory_requirements.resident_bytes
                          or identity.size_bytes or 0))
        if not decision["allowed"]:
            raise CatalogError(decision["reason"],
                               code=decision.get("code", "RESOURCE_DENIED"))
        return self.cache.residency(
            identity.model_id, lambda: backend.load(identity.model_id),
            backend_id=backend.backend_id,
            size_bytes=int(identity.size_bytes or 0),
            fingerprint=identity.artifact_fingerprint, timeout=timeout)

    def _unload_via_backend(self, model_id: str, backend_id: str) -> bool:
        if not backend_id or not self.backends.has(backend_id):
            return False
        try:
            return bool(self.backends.get(backend_id).unload(model_id))
        except Exception:
            return False

    def _governor_load_check(self, identity: ModelIdentity,
                             size: int) -> Dict[str, Any]:
        governor = self.governor
        if governor is None:
            return {"allowed": True, "reason": "no governor attached",
                    "checks": []}
        checks: List[Dict[str, Any]] = []
        try:
            allowed, reason = governor.check_model_load(int(size or 0))
        except Exception as exc:
            return {"allowed": False, "reason": "governor error: %s" % (exc,),
                    "code": "RESOURCE_DENIED", "checks": checks}
        checks.append({"name": "model_memory", "ok": bool(allowed),
                       "detail": reason, "size_bytes": int(size or 0)})
        if not allowed:
            return {"allowed": False, "reason": reason,
                    "code": "RESOURCE_DENIED", "checks": checks}
        try:
            snapshot = governor.snapshot()
            usage = snapshot.get("usage") or {}
            profile = snapshot.get("profile") or {}
            resources = snapshot.get("resources") or {}
        except Exception:
            snapshot, usage, profile, resources = {}, {}, {}, {}
        now = int(usage.get("concurrency_now") or 0)
        capacity = int(usage.get("concurrency_capacity") or 0)
        if capacity and now >= capacity:
            detail = ("all %d governed worker slots are in use" % capacity)
            checks.append({"name": "concurrency", "ok": False,
                           "detail": detail})
            return {"allowed": False, "reason": detail,
                    "code": "CONCURRENCY", "checks": checks}
        checks.append({"name": "concurrency", "ok": True,
                       "detail": "%d/%d slots in use" % (now, capacity)})
        available = resources.get("memory_available_mb")
        required = int(identity.memory_requirements.min_available_mb or 0)
        if available is not None and required and int(available) < required:
            detail = ("model needs %d MB available, %d MB measured"
                      % (required, int(available)))
            checks.append({"name": "ram", "ok": False, "detail": detail})
            return {"allowed": False, "reason": detail, "code": "RAM",
                    "checks": checks}
        checks.append({"name": "ram", "ok": True,
                       "detail": "available=%s MB required=%d MB"
                                 % (available, required)})
        checks.append({"name": "device_profile",
                       "ok": bool(profile.get("model_loading_allowed", True)),
                       "detail": str(profile.get("name") or "")})
        return {"allowed": True, "reason": "within budget",
                "profile": str(profile.get("name") or ""), "checks": checks}

    # -- removal ---------------------------------------------------------

    def remove(self, model_id: str) -> Dict[str, Any]:
        """Remove a model safely; an active request is never invalidated."""
        identity = self.find(model_id)
        key = identity.model_id if identity is not None else model_id
        entry = self.cache.get(key)
        if entry is not None and entry.refs > 0:
            removal = self.cache.remove(key)
            with self._lock:
                if identity is not None:
                    identity.metadata["removal_pending"] = True
                    identity.set_availability(
                        AvailabilityState.UNAVAILABLE.value,
                        reason="removal pending (in use)", strict=False)
            return {"removed": False, "deferred": True, "model_id": key,
                    "refs": entry.refs, "detail": removal,
                    "reason": ("model is referenced by an active request; "
                               "removal is deferred until it is released")}
        if entry is not None:
            self.cache.remove(key)
        with self._lock:
            existed = self._identities.pop(key, None)
        backend = None
        if identity is not None and self.backends.has(identity.backend_id):
            try:
                backend = self.backends.get(identity.backend_id)
                backend.mark_unverified(key)
            except Exception:
                backend = None
        self._emit("remove", model_id=key, existed=existed is not None)
        return {"removed": existed is not None, "model_id": key,
                "backend_id": getattr(backend, "backend_id", "")}

    # -- health / outcomes -----------------------------------------------

    def refresh_health(self, *, backend_id: str = "",
                       probe: bool = True) -> List[BackendStatus]:
        statuses = self.backends.statuses(probe=probe)
        selected = [status for status in statuses
                    if not backend_id or status.backend_id == backend_id]
        for status in selected:
            for identity in self.list(backend_id=status.backend_id):
                identity.record_health({
                    "status": ("ready" if status.ready else
                               ("degraded" if status.reachable
                                else "unavailable")),
                    "backend_id": status.backend_id,
                    "latency_ms": status.latency_ms,
                    "detail": status.detail[:200],
                    "error": status.error[:200],
                    "checked_at": status.checked_at,
                })
        return selected

    def record_outcome(self, model_id: str, *, success: bool,
                       latency_ms: float = 0.0, error: str = "",
                       error_code: str = "", quality: Optional[float] = None,
                       capability: str = "") -> None:
        """Feed telemetry back into routing evidence (metadata only)."""
        identity = self.find(model_id)
        if identity is None:
            return
        if success:
            identity.reliability = min(
                1.0, identity.reliability + (1.0 - identity.reliability) * 0.25)
            if identity.availability_state in (
                    AvailabilityState.DEGRADED.value,) and identity.verified:
                identity.set_availability(AvailabilityState.READY.value,
                                          reason="recovered", strict=False)
        else:
            identity.reliability = max(0.0, identity.reliability * 0.8)
            if error_code in ("TIMEOUT", "timeout"):
                identity.metadata["timeouts"] = int(
                    identity.metadata.get("timeouts") or 0) + 1
            if identity.reliability < 0.2 and \
                    identity.availability_state in (
                        AvailabilityState.READY.value,
                        AvailabilityState.LOADED.value):
                identity.set_availability(AvailabilityState.DEGRADED.value,
                                          reason="reliability below 0.2",
                                          strict=False)
        if latency_ms and latency_ms > 0:
            identity.latency_ms = ((identity.latency_ms + latency_ms) / 2.0
                                   if identity.latency_ms else float(latency_ms))
        if quality is not None:
            identity.quality = min(1.0, max(0.0, float(quality)))
        identity.metadata["last_error"] = error[:200] if error else ""
        identity.metadata["last_capability"] = capability
        self._emit("outcome", model_id=identity.model_id, success=success,
                   latency_ms=latency_ms, error_code=error_code,
                   capability=capability)

    # -- status ----------------------------------------------------------

    def status(self, model_id: str = "") -> Dict[str, Any]:
        if model_id:
            identity = self.get(model_id)
            entry = self.cache.get(identity.model_id)
            return {
                "identity": identity.to_dict(),
                "resident": entry.to_dict() if entry else None,
                "history": [dict(item) for item in identity.history[-8:]],
            }
        with self._lock:
            values = [self._identities[key]
                      for key in sorted(self._identities)]
        by_state: Dict[str, int] = {}
        for identity in values:
            by_state[identity.availability_state] = by_state.get(
                identity.availability_state, 0) + 1
        return {
            "total": len(values),
            "by_availability_state": by_state,
            "verified": sum(1 for item in values if item.verified),
            "usable": sum(1 for item in values if item.usable),
            "local": sum(1 for item in values if item.local),
            "remote": sum(1 for item in values if not item.local),
            "resident": self.cache.snapshot(),
            "backends": [status.to_dict()
                         for status in self.backends.statuses(probe=False)],
            "denials": [dict(item) for item in self._denials[-20:]],
            "models": [item.to_dict() for item in values],
        }

    def snapshot(self) -> Dict[str, Any]:
        return self.status()

    # -- internals -------------------------------------------------------

    def _backend_for(self, identity: ModelIdentity,
                     backend_id: str) -> Optional[Backend]:
        wanted = backend_id or identity.backend_id
        if wanted and self.backends.has(wanted):
            return self.backends.get(wanted)
        for backend in self.backends.list():
            if backend.backend_id == identity.backend_id:
                return backend
        return None

    def _deny(self, code: str, message: str, **extra: Any) -> None:
        record = {"code": code, "message": message[:300], "at": time.time()}
        record.update({key: str(value)[:120] for key, value in extra.items()})
        with self._lock:
            self._denials.append(record)
            if len(self._denials) > 100:
                self._denials = self._denials[-100:]

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.record("catalog.%s" % kind, **payload)
        except Exception:
            pass
