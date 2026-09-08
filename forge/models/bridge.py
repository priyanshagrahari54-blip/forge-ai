"""Model Fabric bridge (A46): the plane's gated path from agent to
model, with honest routing metadata.

The fabric itself (A31) routes/fails-over/health-tracks. This bridge
is the *controlled* entry point: the control plane calls it only
after the MODEL/call permission decision, and it returns structured
metadata so callers always know which model answered, whether the
call succeeded, and — honestly — that the built-in local provider is
a deterministic no-op, not a real model.
"""
from __future__ import annotations

from typing import Any

from forge.models.request import ModelRequest

MAX_PROMPT = 4000
MAX_OUTPUT = 6000

LOCAL_PROVIDER_NAMES = ("local", "local-noop")


class FabricBridge:
    def __init__(self, fabric: Any) -> None:
        self.fabric = fabric

    def generate(self, prompt: str, *, capability: str = "coding",
                 context: str = "") -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        request = ModelRequest(
            prompt=prompt.strip()[:MAX_PROMPT],
            capability=capability or "coding",
            context=(context or "")[:8000])
        response = self.fabric.generate(request)
        simulated = self._provider_is_simulated(response.provider)
        try:
            info = self.fabric.providers.info(response.provider)
            provider_kind = info.kind
        except Exception:
            provider_kind = ("local" if simulated else "remote")
        return {
            "prompt": request.prompt[:400],
            "capability": request.capability,
            "model": response.model,
            "provider": response.provider,
            "provider_kind": provider_kind,
            "simulated": simulated,
            "text": response.text[:MAX_OUTPUT],
            "success": bool(response.success),
            "error": response.error[:400],
            "latency_ms": round(response.latency_ms, 2),
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "note": ("Local provider responses are a deterministic no-op "
                     "that refuses to fabricate; configure a real "
                     "provider for actual generation.")
            if simulated else "",
        }

    def _provider_is_simulated(self, provider_name: str) -> bool:
        if provider_name in LOCAL_PROVIDER_NAMES:
            return True
        try:
            info = self.fabric.providers.info(provider_name)
        except Exception:
            return False
        return getattr(info, "kind", "") in ("mock", "fallback")
