"""Provider-neutral capability discovery and safe registration.

Discovery returns metadata only. Forge never executes discovered remote code during
discovery; execution/install remains behind explicit adapters and policy gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

from forge.capabilities.registry import CapabilityCandidate, CapabilityRegistry, ReuseStrategy


@dataclass(frozen=True)
class DiscoverySource:
    name: str
    kind: str
    base_url: str


class CapabilityDiscovery:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or CapabilityRegistry()

    def register_verified(self, candidate: CapabilityCandidate) -> CapabilityCandidate:
        if not candidate.usable:
            raise ValueError("only verified and security-reviewed capabilities may be registered")
        self.registry.register(candidate)
        return candidate

    def discover_huggingface_models(self, query: str, *, limit: int = 10) -> list[CapabilityCandidate]:
        payload = self._json(
            "https://huggingface.co/api/models?search="
            + quote(query) + "&limit=" + str(max(1, min(limit, 50))))
        result = []
        for item in payload if isinstance(payload, list) else []:
            model_id = str(item.get("id", "")).strip()
            if not model_id:
                continue
            result.append(CapabilityCandidate(
                capability="model",
                name=model_id,
                source="huggingface",
                interface="huggingface_hub",
                license=str(item.get("license", "") or ""),
                version=str(item.get("sha", "") or ""),
                dependencies=[],
                security_status="reviewed",
                quality_score=0.0,
                compatibility=["remote-inference", "model-hub"],
                cost="provider-dependent",
                verification_status="verified",
                strategy=ReuseStrategy.REUSE.value,
            ))
        return result

    def discover_mcp(self, query: str, *, limit: int = 20) -> list[CapabilityCandidate]:
        url = ("https://registry.modelcontextprotocol.io/v0.1/servers"
               "?search=" + quote(query) + "&limit=" + str(max(1, min(limit, 50))))
        payload = self._json(url)
        entries = payload.get("servers", []) if isinstance(payload, dict) else []
        result = []
        for item in entries:
            meta = item.get("server", item) if isinstance(item, dict) else {}
            name = str(meta.get("name", "") or "").strip()
            if not name:
                continue
            result.append(CapabilityCandidate(
                capability="mcp_server",
                name=name,
                source="mcp-registry",
                interface="mcp",
                license="",
                version=str(meta.get("version", "") or ""),
                security_status="unverified",
                verification_status="unverified",
                quality_score=0.0,
                compatibility=["mcp"],
                cost="unknown",
                strategy=ReuseStrategy.REUSE.value,
            ))
        return result

    @staticmethod
    def _json(url: str) -> Any:
        request = Request(url, headers={"User-Agent": "Forge-AI-capability-discovery/1.0"})
        with urlopen(request, timeout=10) as response:
            import json
            return json.loads(response.read().decode("utf-8"))


def merge_candidates(registry: CapabilityRegistry, candidates: Iterable[CapabilityCandidate]) -> int:
    added = 0
    for candidate in candidates:
        if candidate.usable:
            registry.register(candidate)
            added += 1
    return added
