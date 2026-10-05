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
                capability="model", name=model_id, source="huggingface",
                interface="huggingface_hub",
                license=str(item.get("license", "") or ""),
                version=str(item.get("sha", "") or ""),
                dependencies=[],
                security_status="unverified", quality_score=0.0,
                compatibility=["remote-inference", "model-hub"],
                cost="provider-dependent", verification_status="unverified",
                strategy=ReuseStrategy.REUSE.value,
            ))
        return result

    def discover_huggingface_datasets(self, query: str, *, limit: int = 10) -> list[CapabilityCandidate]:
        return self._discover_huggingface_repo_type(
            query, limit=limit, repo_type="datasets", capability="dataset")

    def discover_huggingface_spaces(self, query: str, *, limit: int = 10) -> list[CapabilityCandidate]:
        return self._discover_huggingface_repo_type(
            query, limit=limit, repo_type="spaces", capability="space")

    def _discover_huggingface_repo_type(
        self, query: str, *, limit: int, repo_type: str, capability: str
    ) -> list[CapabilityCandidate]:
        payload = self._json(
            "https://huggingface.co/api/" + repo_type + "?search=" + quote(query)
            + "&limit=" + str(max(1, min(limit, 50))))
        result = []
        for item in payload if isinstance(payload, list) else []:
            repo_id = str(item.get("id", "")).strip()
            if not repo_id:
                continue
            result.append(CapabilityCandidate(
                capability=capability, name=repo_id, source="huggingface",
                interface="huggingface_hub",
                license=str(item.get("license", "") or ""),
                version=str(item.get("sha", "") or ""),
                dependencies=[],
                security_status="unverified", quality_score=0.0,
                compatibility=["remote-repository", repo_type],
                cost="provider-dependent", verification_status="unverified",
                strategy=ReuseStrategy.REUSE.value,
            ))
        return result

    def discover_github_repositories(self, query: str, *, limit: int = 10) -> list[CapabilityCandidate]:
        """Discover repositories only; never treats GitHub metadata as a trust decision."""
        payload = self._json(
            "https://api.github.com/search/repositories?q=" + quote(query)
            + "&per_page=" + str(max(1, min(limit, 50))))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        result = []
        for item in items:
            full_name = str(item.get("full_name", "")).strip()
            if not full_name:
                continue
            result.append(CapabilityCandidate(
                capability="repository",
                name=full_name,
                source="github",
                interface="git",
                license=str((item.get("license") or {}).get("spdx_id", "") or ""),
                version=str(item.get("default_branch", "") or ""),
                dependencies=[],
                security_status="unverified",
                quality_score=0.0,
                compatibility=["git", "source-repository"],
                cost="free-to-access",
                verification_status="unverified",
                strategy=ReuseStrategy.ADAPT.value,
            ))
        return result

    def discover_package_candidates(self, query: str, *, limit: int = 10) -> list[CapabilityCandidate]:
        """Discover PyPI packages as metadata; installation is a separate verified step."""
        payload = self._json(
            "https://pypi.org/pypi/" + quote(query.strip()) + "/json")
        if not isinstance(payload, dict) or not payload.get("info"):
            return []
        info = payload["info"]
        name = str(info.get("name", "")).strip()
        if not name:
            return []
        return [CapabilityCandidate(
            capability="package",
            name=name,
            source="pypi",
            interface="python-package",
            license=str(info.get("license", "") or ""),
            version=str(info.get("version", "") or ""),
            dependencies=list(info.get("requires_dist") or []),
            security_status="unverified",
            quality_score=0.0,
            compatibility=["python", "pypi"],
            cost="free-to-access",
            verification_status="unverified",
            strategy=ReuseStrategy.REUSE.value,
        )][:max(1, min(limit, 50))]

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
                capability="mcp_server", name=name, source="mcp-registry",
                interface="mcp", license="", version=str(meta.get("version", "") or ""),
                security_status="unverified", verification_status="unverified",
                quality_score=0.0, compatibility=["mcp"], cost="unknown",
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
