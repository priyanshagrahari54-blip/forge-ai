"""Provider outcome/health vocabulary shared by all external integrations.

Two explicit vocabularies (Phases 2/11):

- :class:`ProviderState` — the outcome of ONE provider call. A
  non-``SUCCESS`` state never means the request succeeded, and
  ``simulation=false`` alone never implies success: callers must check
  ``state == SUCCESS`` (or the ``ok`` boolean set from it).
- :class:`ProviderHealth` — the standing capability of a provider as
  reported to the cockpit/router: AVAILABLE / DEGRADED /
  RATE_LIMITED / AUTH_FAILED / UNAVAILABLE / MISCONFIGURED.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

#: Explicit outcomes for a single provider request.
#: ``simulation=false`` must NEVER mean "request succeeded": a failed
#: real request reports one of the error states below.
PROVIDER_STATES = (
    "SUCCESS",          # real, successful provider response
    "PROVIDER_ERROR",   # provider returned an error (5xx, bad payload)
    "TIMEOUT",          # request exceeded the timeout budget
    "RATE_LIMITED",     # provider rate-limited the request (429)
    "AUTH_ERROR",       # credentials missing/invalid (401/403)
    "POLICY_DENIED",    # refused locally by Forge policy/validation
    "UNAVAILABLE",      # provider not configured or unreachable
)

#: Standing health states surfaced to the cockpit and the router.
PROVIDER_HEALTH_STATES = (
    "AVAILABLE",        # configured and believed operational
    "DEGRADED",         # configured but failing intermittently
    "RATE_LIMITED",     # currently throttled by the provider
    "AUTH_FAILED",      # configured but credentials rejected
    "UNAVAILABLE",      # not configured or unreachable
    "MISCONFIGURED",    # configured but invalid settings
)


class ProviderState(str, Enum):
    SUCCESS = "SUCCESS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def ok(self) -> bool:
        return self is ProviderState.SUCCESS

    @property
    def retryable(self) -> bool:
        return self in (ProviderState.TIMEOUT,
                        ProviderState.RATE_LIMITED,
                        ProviderState.PROVIDER_ERROR)


class ProviderHealth(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"


def classify_http_status(status: int | None) -> ProviderState:
    """Map an HTTP status to the explicit provider outcome."""
    if status is None:
        return ProviderState.PROVIDER_ERROR
    if status in (401, 403):
        return ProviderState.AUTH_ERROR
    if status == 408:
        return ProviderState.TIMEOUT
    if status == 429:
        return ProviderState.RATE_LIMITED
    if 400 <= status < 500:
        return ProviderState.PROVIDER_ERROR
    if 500 <= status <= 599:
        return ProviderState.PROVIDER_ERROR
    return ProviderState.PROVIDER_ERROR


def health_for(state: ProviderState | str) -> ProviderHealth:
    """Fold one call outcome into a health reading (best-effort)."""
    state = state if isinstance(state, ProviderState) \
        else ProviderState(str(state))
    if state is ProviderState.SUCCESS:
        return ProviderHealth.AVAILABLE
    if state is ProviderState.AUTH_ERROR:
        return ProviderHealth.AUTH_FAILED
    if state is ProviderState.RATE_LIMITED:
        return ProviderHealth.RATE_LIMITED
    if state is ProviderState.UNAVAILABLE:
        return ProviderHealth.UNAVAILABLE
    return ProviderHealth.DEGRADED


#: Standing provider-status ladder for the final gate (A80). This is
#: the vocabulary that answers "may this provider be treated as
#: production-capable?" — an environment variable alone is NEVER
#: enough; a provider is VERIFIED only after an explicit, successful,
#: recent capability verification (see ``forge.final.provider_verification``).
#:
#: Ladder semantics:
#:   NOT_CONFIGURED   — no configuration present (nothing to verify)
#:   CONFIGURED       — configuration present, capability NOT yet
#:                      verified (and no fresh failed attempt)
#:   AUTHENTICATING   — verification in progress (credential check)
#:   AUTHENTICATED    — credentials accepted (transient stage)
#:   REACHABLE        — endpoint reachable (transient stage)
#:   HEALTHY          — standing good state (transient stage)
#:   VERIFIED         — full capability verification succeeded and is
#:                      recent enough per the documented TTL policy
#:   DEGRADED         — previously verified but failing intermittently
#:                      (e.g. fabric router feedback after failures)
#:   UNAVAILABLE      — configured but unreachable / DNS failure
#:   MISCONFIGURED    — configuration present but invalid/refused by
#:                      policy (e.g. SSH host without allowlist)
#:   PROVIDER_ERROR   — a fresh verification attempt ended in a
#:                      provider-side error (5xx, bad payload)
#:   TIMEOUT / RATE_LIMITED / AUTH_ERROR / POLICY_DENIED — a fresh
#:                      verification attempt ended in that specific
#:                      outcome; AUTH_ERROR also covers credentials
#:                      rejected during verification.
PROVIDER_STATUS_LADDER = (
    "NOT_CONFIGURED", "CONFIGURED", "AUTHENTICATING", "AUTHENTICATED",
    "REACHABLE", "HEALTHY", "VERIFIED", "DEGRADED", "UNAVAILABLE",
    "MISCONFIGURED", "PROVIDER_ERROR", "TIMEOUT", "RATE_LIMITED",
    "AUTH_ERROR", "POLICY_DENIED",
)


class ProviderStatus(str, Enum):
    """Standing status of one provider as judged by the A80 gate.

    ``CONFIGURED`` never implies capability: it only records that
    configuration exists. Only ``VERIFIED`` (with fresh evidence)
    qualifies a provider for PRODUCTION_READY.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    REACHABLE = "REACHABLE"
    HEALTHY = "HEALTHY"
    VERIFIED = "VERIFIED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    POLICY_DENIED = "POLICY_DENIED"

    @property
    def verified(self) -> bool:
        return self is ProviderStatus.VERIFIED

    @property
    def failure(self) -> bool:
        """True when this status is an unresolved provider failure."""
        return self in (
            ProviderStatus.UNAVAILABLE, ProviderStatus.PROVIDER_ERROR,
            ProviderStatus.TIMEOUT, ProviderStatus.RATE_LIMITED,
            ProviderStatus.AUTH_ERROR, ProviderStatus.POLICY_DENIED,
        )


def state_dict(state: ProviderState | str, **extra: Any) -> dict[str, Any]:
    """Serialization helper for provider outcomes."""
    state = state if isinstance(state, ProviderState) \
        else ProviderState(str(state))
    payload: dict[str, Any] = {
        "state": state.value,
        "ok": state.ok,
        **extra,
    }
    return payload
