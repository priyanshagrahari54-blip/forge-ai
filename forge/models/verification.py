"""Real model verification (Session 11).

Verification is what separates ``CONFIGURED`` from ``READY``. It is never
satisfied by configuration: an endpoint in a config file, an API key in the
environment, or a file that exists on disk all produce ``UNVERIFIED``.

Three real checks, in order:

1. **Artifact fingerprint** (local models): a bounded sha256 over the actual
   bytes. A fingerprint that differs from the registered one is tampering or
   substitution and fails verification.
2. **Backend reachability**: a live health probe must report the backend
   reachable. ``probe=False`` can never verify anything.
3. **Identity probe**: one bounded *real* generation. The response must
   succeed, be non-empty, and carry the identity that was requested
   (:meth:`~forge.models.identity.ModelIdentity.assert_same_model`) — a
   backend that answers with a different model fails as spoofed.

Nothing here stores prompt or completion text: only lengths, timings, states,
and fingerprints. Verification never changes a permission, never authorizes a
network destination, and never grants capability — it can only refuse.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from forge.models.identity import (AvailabilityState, ModelIdentity,
                                   ModelSpoofingError, VerificationState)

__all__ = [
    "MAX_FINGERPRINT_BYTES",
    "VerificationCheck",
    "VerificationResult",
    "ModelVerifier",
    "fingerprint_artifact",
]

#: Bound on how much of an artifact is hashed. A 64 MiB prefix is far more
#: than any reference artifact and keeps verification cheap on a thin client;
#: the *reported* fingerprint states the bound so it is never mistaken for a
#: whole-file digest of a larger file.
MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024

#: Default probe: bounded, harmless, and clearly a probe.
DEFAULT_PROBE_PROMPT = "Forge model verification probe. Reply briefly."
DEFAULT_PROBE_OUTPUT = 24
DEFAULT_PROBE_TIMEOUT = 30.0


def fingerprint_artifact(path: str, *,
                         max_bytes: int = MAX_FINGERPRINT_BYTES
                         ) -> Tuple[str, int, str]:
    """Return ``(sha256_hex, bytes_hashed, note)`` for a local artifact.

    ``("", 0, reason)`` when the file cannot be read. Never raises.
    """
    if not path or not os.path.isfile(path):
        return ("", 0, "no local artifact to fingerprint")
    digest = hashlib.sha256()
    hashed = 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            while hashed < max_bytes:
                block = handle.read(min(1024 * 1024, max_bytes - hashed))
                if not block:
                    break
                digest.update(block)
                hashed += len(block)
    except OSError as exc:
        return ("", 0, "artifact unreadable: %s" % (exc,))
    note = ""
    if size > max_bytes:
        note = ("partial fingerprint: first %d of %d bytes" % (max_bytes, size))
    return (digest.hexdigest(), hashed, note)


@dataclass
class VerificationCheck:
    """One named check inside a verification."""

    name: str
    ok: bool
    detail: str = ""
    #: ``skipped`` when the check does not apply (e.g. no local artifact).
    skipped: bool = False
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": bool(self.ok),
                "skipped": bool(self.skipped),
                "detail": self.detail[:400],
                "duration_ms": round(float(self.duration_ms or 0.0), 3)}


@dataclass
class VerificationResult:
    """The outcome of a verification attempt."""

    model_id: str
    verified: bool = False
    state: str = VerificationState.UNVERIFIED.value
    method: str = ""
    detail: str = ""
    fingerprint: str = ""
    fingerprint_bytes: int = 0
    latency_ms: float = 0.0
    checked_at: float = field(default_factory=time.time)
    #: Length of the probe completion — never its text.
    probe_chars: int = 0
    probe_model: str = ""
    error: str = ""
    error_code: str = ""
    checks: List[VerificationCheck] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "verified": bool(self.verified),
            "state": self.state,
            "method": self.method,
            "detail": self.detail[:400],
            "fingerprint": self.fingerprint,
            "fingerprint_bytes": int(self.fingerprint_bytes or 0),
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "checked_at": self.checked_at,
            "probe_chars": int(self.probe_chars or 0),
            "probe_model": self.probe_model,
            "error": self.error[:400],
            "error_code": self.error_code,
            "checks": [check.to_dict() for check in self.checks],
        }

    def failures(self) -> List[VerificationCheck]:
        return [check for check in self.checks
                if not check.ok and not check.skipped]


class ModelVerifier:
    """Runs real verification against a fabric backend."""

    def __init__(self, *, probe_prompt: str = DEFAULT_PROBE_PROMPT,
                 probe_output: int = DEFAULT_PROBE_OUTPUT,
                 probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
                 ttl_seconds: float = 0.0,
                 fingerprint_bound: int = MAX_FINGERPRINT_BYTES,
                 require_probe: bool = True) -> None:
        self.probe_prompt = probe_prompt
        self.probe_output = max(1, min(int(probe_output or 1), 256))
        self.probe_timeout = max(0.5, float(probe_timeout or 1.0))
        self.ttl_seconds = max(0.0, float(ttl_seconds or 0.0))
        self.fingerprint_bound = max(1024, int(fingerprint_bound or 1024))
        self.require_probe = bool(require_probe)

    # -- entry point -----------------------------------------------------

    def verify(self, identity: ModelIdentity, backend: Any, *,
               request_factory: Any = None,
               expected_fingerprint: str = "") -> VerificationResult:
        """Verify one identity through one backend. Never raises."""
        from forge.runtime.model_runtime import RuntimeRequest

        started = time.perf_counter()
        result = VerificationResult(model_id=identity.model_id)
        factory = request_factory or RuntimeRequest

        # 1. artifact fingerprint (local only) ---------------------------
        expected = expected_fingerprint or identity.artifact_fingerprint
        path = identity.artifact_path
        if identity.local and path:
            check_started = time.perf_counter()
            digest, hashed, note = fingerprint_artifact(
                path, max_bytes=self.fingerprint_bound)
            if not digest:
                result.checks.append(VerificationCheck(
                    "artifact_fingerprint", False, note or "unreadable",
                    duration_ms=_ms(check_started)))
                return self._finish(result, identity, started,
                                    code="ARTIFACT_UNREADABLE")
            result.fingerprint = digest
            result.fingerprint_bytes = hashed
            detail = "%d bytes hashed" % hashed
            if note:
                detail += " (%s)" % note
            if expected and expected != digest:
                result.checks.append(VerificationCheck(
                    "artifact_fingerprint", False,
                    "fingerprint %s does not match the registered %s"
                    % (digest[:16], expected[:16]),
                    duration_ms=_ms(check_started)))
                return self._finish(result, identity, started,
                                    code="FINGERPRINT_MISMATCH")
            result.checks.append(VerificationCheck(
                "artifact_fingerprint", True, detail,
                duration_ms=_ms(check_started)))
        else:
            result.checks.append(VerificationCheck(
                "artifact_fingerprint", True,
                "remote model: no local artifact", skipped=True))

        # 2. backend reachability ----------------------------------------
        check_started = time.perf_counter()
        try:
            status = backend.health(probe=True)
        except Exception as exc:
            status = None
            detail = "health probe raised: %s" % (_text(exc),)
            result.checks.append(VerificationCheck(
                "backend_reachable", False, detail,
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="BACKEND_UNREACHABLE")
        reachable = bool(getattr(status, "reachable", False))
        detail = str(getattr(status, "detail", "") or "")
        denial = str(getattr(status, "denial", "") or "")
        if denial:
            result.checks.append(VerificationCheck(
                "backend_reachable", False, "denied: %s" % denial,
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="NETWORK_POLICY")
        if not reachable:
            result.checks.append(VerificationCheck(
                "backend_reachable", False,
                detail or "backend did not report reachable",
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="BACKEND_UNREACHABLE")
        result.checks.append(VerificationCheck(
            "backend_reachable", True, detail or "reachable",
            duration_ms=_ms(check_started)))

        # 3. identity probe: one real generation --------------------------
        if not self.require_probe:
            result.method = "fingerprint+health"
            result.detail = "probe disabled by configuration"
            result.verified = True
            result.state = VerificationState.VERIFIED.value
            return self._finish(result, identity, started)

        check_started = time.perf_counter()
        try:
            request = factory(
                prompt=self.probe_prompt,
                model=identity.model_id,
                backend=getattr(backend, "backend_name",
                                getattr(backend, "backend_id", "")),
                max_output_tokens=self.probe_output,
                timeout=self.probe_timeout,
            )
            response = backend.generate(request)
        except ModelSpoofingError as exc:
            result.checks.append(VerificationCheck(
                "identity_probe", False, str(exc),
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="MODEL_SPOOFED", error=str(exc))
        except Exception as exc:
            result.checks.append(VerificationCheck(
                "identity_probe", False, "probe failed: %s" % (_text(exc),),
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="PROBE_FAILED", error=_text(exc))

        success = bool(getattr(response, "success", False))
        text = str(getattr(response, "text", "") or "")
        reported_model = str(getattr(response, "model", "") or "")
        result.probe_chars = len(text)
        result.probe_model = reported_model
        if not success:
            error = str(getattr(response, "error", "") or "probe failed")
            result.checks.append(VerificationCheck(
                "identity_probe", False,
                "backend reported failure: %s" % error[:300],
                duration_ms=_ms(check_started)))
            code = str(getattr(response, "error_kind", "") or "").upper()
            return self._finish(result, identity, started,
                                code=("TIMEOUT" if "TIMEOUT" in code
                                      else "PROBE_FAILED"), error=error)
        if not text.strip():
            result.checks.append(VerificationCheck(
                "identity_probe", False,
                "probe produced an empty completion",
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="EMPTY_COMPLETION")
        try:
            identity.assert_same_model(reported_model=reported_model)
        except ModelSpoofingError as exc:
            result.checks.append(VerificationCheck(
                "identity_probe", False, str(exc),
                duration_ms=_ms(check_started)))
            return self._finish(result, identity, started,
                                code="MODEL_SPOOFED", error=str(exc))
        result.checks.append(VerificationCheck(
            "identity_probe", True,
            "real completion of %d chars from %r"
            % (len(text), reported_model or identity.model_id),
            duration_ms=_ms(check_started)))

        result.verified = True
        result.state = VerificationState.VERIFIED.value
        result.method = "fingerprint+health+probe"
        result.detail = ("verified by a real generation on backend %r"
                         % getattr(backend, "backend_id", ""))
        return self._finish(result, identity, started)

    # -- helpers ---------------------------------------------------------

    def _finish(self, result: VerificationResult, identity: ModelIdentity,
                started: float, *, code: str = "", error: str = "") -> Any:
        result.latency_ms = _ms(started)
        result.checked_at = time.time()
        if not result.verified:
            result.state = VerificationState.FAILED.value
            result.error_code = code or "VERIFICATION_FAILED"
            if error and not result.error:
                result.error = error
            if not result.detail:
                failures = result.failures()
                result.detail = (failures[0].detail if failures
                                 else "verification did not complete")
        # Apply the outcome to the identity so state stays coherent.
        identity.set_verification(
            VerificationState.VERIFIED.value if result.verified
            else VerificationState.FAILED.value,
            method=result.method or result.error_code,
            detail=result.detail,
            fingerprint=result.fingerprint,
            ttl_seconds=self.ttl_seconds or None)
        if result.verified:
            if identity.availability_state in (
                    AvailabilityState.LOADED.value,
                    AvailabilityState.CONFIGURED.value,
                    AvailabilityState.UNVERIFIED.value,
                    AvailabilityState.DEGRADED.value):
                identity.set_availability(AvailabilityState.READY.value,
                                          reason="verification passed")
        else:
            if identity.availability_state not in (
                    AvailabilityState.POLICY_DENIED.value,
                    AvailabilityState.RESOURCE_DENIED.value):
                identity.set_availability(AvailabilityState.UNVERIFIED.value,
                                          reason=result.error_code,
                                          strict=False)
        return result


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _text(exc: BaseException) -> str:
    try:
        from forge.runtime.model_runtime import redact_text
        return redact_text(str(exc))[:400]
    except Exception:
        return str(exc)[:400]
