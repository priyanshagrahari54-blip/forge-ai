"""Routing policy for the Model Fabric.

The policy encodes Forge's cost/free/local posture and the deterministic
fallback ladder. Preferences (free, local) are soft weights; ``allow_*`` flags
and numeric bounds are hard filters that the fallback ladder relaxes in a fixed,
documented order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Ordered relaxation steps tried when no model satisfies the strict policy.
#: Each step removes one hard constraint; capability requirements are never
#: relaxed (a vision request is never silently sent to a text-only model).
DEFAULT_FALLBACK_ORDER: tuple[str, ...] = (
    "latency",       # drop max latency
    "reliability",   # drop minimum reliability
    "remote",        # allow remote providers
    "paid",          # allow paid providers
    "health",        # allow degraded/unhealthy models as a last resort
)


@dataclass
class RoutingPolicy:
    """Cost/free/local policy plus deterministic fallback configuration."""

    prefer_free: bool = True
    prefer_local: bool = True
    allow_remote: bool = True
    allow_paid: bool = True
    max_cost_per_token: float | None = None
    max_latency_ms: float | None = None
    min_reliability: float = 0.0
    #: A request whose required capabilities no model supports fails fast
    #: rather than being routed to an incapable model.
    require_capabilities: bool = True
    #: Relaxation steps attempted, in order, when strict routing finds nothing.
    fallback_order: tuple[str, ...] = DEFAULT_FALLBACK_ORDER
    #: Allow fallback to relax cost/privacy constraints after the configured
    #: ladder. True means "best effort"; False means "fail rather than breach
    #: the privacy/cost posture".
    use_best_effort: bool = True

    def validate(self) -> None:
        if self.min_reliability < 0.0 or self.min_reliability > 1.0:
            raise ValueError("min_reliability must be within [0, 1]")
        if self.max_cost_per_token is not None and self.max_cost_per_token < 0:
            raise ValueError("max_cost_per_token cannot be negative")
        if self.max_latency_ms is not None and self.max_latency_ms < 0:
            raise ValueError("max_latency_ms cannot be negative")
        for step in self.fallback_order:
            if step not in DEFAULT_FALLBACK_ORDER:
                raise ValueError(f"Unknown fallback step: {step!r}")

    # -- named presets ---------------------------------------------------

    @classmethod
    def preset(cls, name: str) -> "RoutingPolicy":
        """Return a named preset policy.

        Supported presets: ``quality``, ``balanced``, ``fast``, ``free``,
        ``local``, ``privacy``. Unknown names raise ``ValueError``.
        """
        presets = {
            "quality": cls(
                prefer_free=False,
                prefer_local=False,
                allow_remote=True,
                allow_paid=True,
                min_reliability=0.5,
            ),
            "balanced": cls(),
            "fast": cls(
                prefer_free=False,
                prefer_local=False,
                max_latency_ms=2000.0,
            ),
            "free": cls(
                prefer_free=True,
                allow_paid=False,
                max_cost_per_token=0.0,
            ),
            "local": cls(prefer_local=True, allow_remote=False),
            "privacy": cls(
                prefer_local=True,
                allow_remote=False,
                allow_paid=False,
                use_best_effort=False,
            ),
        }
        key = (name or "").strip().lower()
        if key not in presets:
            raise ValueError(
                f"Unknown policy preset {name!r}; choose from {sorted(presets)}"
            )
        return presets[key]

    def resolve_for_request(
        self,
        *,
        prefer_free: bool | None = None,
        prefer_local: bool | None = None,
        allow_remote: bool | None = None,
        allow_paid: bool | None = None,
        max_cost_per_token: float | None = None,
        max_latency_ms: float | None = None,
    ) -> "RoutingPolicy":
        """Return a policy with per-request overrides applied (no mutation)."""
        return RoutingPolicy(
            prefer_free=self.prefer_free if prefer_free is None else prefer_free,
            prefer_local=self.prefer_local if prefer_local is None else prefer_local,
            allow_remote=self.allow_remote if allow_remote is None else allow_remote,
            allow_paid=self.allow_paid if allow_paid is None else allow_paid,
            max_cost_per_token=(
                self.max_cost_per_token if max_cost_per_token is None else max_cost_per_token
            ),
            max_latency_ms=self.max_latency_ms if max_latency_ms is None else max_latency_ms,
            min_reliability=self.min_reliability,
            require_capabilities=self.require_capabilities,
            fallback_order=self.fallback_order,
            use_best_effort=self.use_best_effort,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefer_free": self.prefer_free,
            "prefer_local": self.prefer_local,
            "allow_remote": self.allow_remote,
            "allow_paid": self.allow_paid,
            "max_cost_per_token": self.max_cost_per_token,
            "max_latency_ms": self.max_latency_ms,
            "min_reliability": self.min_reliability,
            "require_capabilities": self.require_capabilities,
            "fallback_order": list(self.fallback_order),
            "use_best_effort": self.use_best_effort,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RoutingPolicy":
        if not data:
            return cls()
        return cls(
            prefer_free=bool(data.get("prefer_free", True)),
            prefer_local=bool(data.get("prefer_local", True)),
            allow_remote=bool(data.get("allow_remote", True)),
            allow_paid=bool(data.get("allow_paid", True)),
            max_cost_per_token=data.get("max_cost_per_token"),
            max_latency_ms=data.get("max_latency_ms"),
            min_reliability=float(data.get("min_reliability", 0.0)),
            require_capabilities=bool(data.get("require_capabilities", True)),
            fallback_order=tuple(data.get("fallback_order", DEFAULT_FALLBACK_ORDER)),
            use_best_effort=bool(data.get("use_best_effort", True)),
        )
