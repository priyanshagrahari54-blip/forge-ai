"""Explicit provider/runtime configuration state."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional


class RuntimeState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED = "CONFIGURED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    VERIFIED = "VERIFIED"
    LIVE = "LIVE"
    UNAVAILABLE = "UNAVAILABLE"
    REVOKED = "REVOKED"


@dataclass
class ConfiguredRuntime:
    """Non-secret runtime evidence for one provider/model binding."""

    provider: str
    model_id: str = ""
    endpoint: str = ""
    state: str = RuntimeState.UNCONFIGURED.value
    capabilities: tuple[str, ...] = ()
    verification_id: str = ""
    last_reason: str = ""
    last_checked: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def set_configured(self, *, valid: bool = True, reason: str = "") -> None:
        if valid:
            self.state = RuntimeState.CONFIGURED.value
            self.last_reason = ""
        else:
            self.state = RuntimeState.INVALID_CONFIGURATION.value
            self.last_reason = _bounded_reason(reason)

    def mark_verified(self, *, verification_id: str = "",
                      capabilities: Iterable[str] = (),
                      checked_at: float = 0.0) -> None:
        if self.state != RuntimeState.CONFIGURED.value:
            raise RuntimeError("runtime must be CONFIGURED before verification")
        self.state = RuntimeState.VERIFIED.value
        self.verification_id = str(verification_id or "")
        self.capabilities = tuple(
            dict.fromkeys(str(x) for x in capabilities if str(x)))
        self.last_reason = ""
        self.last_checked = float(checked_at or 0.0)

    def activate(self) -> None:
        if self.state != RuntimeState.VERIFIED.value:
            raise RuntimeError("only a VERIFIED runtime may become LIVE")
        self.state = RuntimeState.LIVE.value

    def mark_unavailable(self, reason: str = "") -> None:
        self.state = RuntimeState.UNAVAILABLE.value
        self.last_reason = _bounded_reason(reason)

    def revoke(self, reason: str = "") -> None:
        self.state = RuntimeState.REVOKED.value
        self.last_reason = _bounded_reason(reason)

    @property
    def routable(self) -> bool:
        return self.state == RuntimeState.LIVE.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "endpoint": _safe_endpoint(self.endpoint),
            "state": self.state,
            "capabilities": list(self.capabilities),
            "verification_id": self.verification_id,
            "last_reason": self.last_reason,
            "last_checked": self.last_checked,
        }


class ConfiguredRuntimeRegistry:
    """In-memory authority for non-secret runtime state."""

    def __init__(self) -> None:
        self._items: Dict[str, ConfiguredRuntime] = {}

    @staticmethod
    def key(provider: str, model_id: str = "") -> str:
        return "%s:%s" % (str(provider).strip(), str(model_id).strip())

    def register(self, runtime: ConfiguredRuntime) -> ConfiguredRuntime:
        if not runtime.provider.strip():
            raise ValueError("provider is required")
        key = self.key(runtime.provider, runtime.model_id)
        if key in self._items:
            raise ValueError("runtime already registered: %s" % key)
        self._items[key] = runtime
        return runtime

    def get(self, provider: str, model_id: str = "") -> ConfiguredRuntime:
        resolved = self.resolve_key(provider, model_id)
        return self._items[resolved]

    def maybe_get(self, provider: str, model_id: str = "") -> Optional[ConfiguredRuntime]:
        return self._items.get(self.resolve_key(provider, model_id))

    @classmethod
    def resolve_key(cls, provider: str, model_id: str = "") -> str:
        """Resolve a lookup to a registry key.

        Two call styles are supported and unambiguous:

        * ``resolve_key("fake", "model-a")`` / ``resolve_key("fake")`` — the
          explicit ``provider``/``model_id`` form, where the empty
          ``model_id`` addresses the provider-wide entry (``"fake:"``).
        * ``resolve_key("fake:model-a")`` — the composite form used by the
          rest of the runtime API (snapshots, logs, API responses). A bare
          ``provider:model`` string with no explicit ``model_id`` addresses
          that exact recording.

        A literal key always wins, so nothing that used to resolve stops
        resolving.
        """
        if model_id or ":" not in str(provider):
            return cls.key(provider, model_id)
        return str(provider).strip()

    def items(self) -> list:
        return [self._items[key] for key in sorted(self._items)]

    def live(self) -> list:
        return [item for item in self._items.values() if item.routable]

    def snapshot(self) -> list:
        return [self._items[key].to_dict() for key in sorted(self._items)]

    def counts(self) -> Dict[str, int]:
        result = {state.value: 0 for state in RuntimeState}
        for item in self._items.values():
            result[item.state] = result.get(item.state, 0) + 1
        return result


def _bounded_reason(reason: str, limit: int = 500) -> str:
    text = " ".join(str(reason or "").split())
    return text[:limit]


def _safe_endpoint(endpoint: str) -> str:
    value = str(endpoint or "")
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    if "@" not in rest:
        return value
    return scheme + "://" + rest.split("@", 1)[1]
