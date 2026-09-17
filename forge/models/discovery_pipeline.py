"""Persistent discovery pipeline for real model identities.

Discovery and runtime availability are intentionally separate:
- discovery may catalogue concrete public model IDs;
- registration adds them as non-live Fabric entries;
- verification can activate an exact ID only when a caller supplies a real
  runtime probe and that probe succeeds.

No model name, capability, or live state is fabricated by this module.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from forge.models.oss_fabric import register_oss_catalog
from forge.models.oss_registry import OSSModel, discover_huggingface_models, oss_catalog
from forge.models.registry import Model, ModelRegistry
from forge.models.runtime_verification import RuntimeProbeResult, verify_model


@dataclass(frozen=True)
class DiscoverySnapshot:
    schema_version: int
    discovered: int
    registered: int
    verified: int
    live: int
    target: int
    source: str

    @property
    def meets_target(self) -> bool:
        return self.discovered >= self.target

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"meets_target": self.meets_target}


class RealModelDiscoveryPipeline:
    """Coordinate discovery, durable inventory, registration and verification."""

    def __init__(self, registry: ModelRegistry, *, state_path: str | Path = ".forge/real-models.json",
                 target: int = 1000) -> None:
        if target < 1:
            raise ValueError("target must be positive")
        self.registry = registry
        self.state_path = Path(state_path)
        self.target = target
        self.inventory: dict[str, OSSModel] = {}
        self._load()

    def discover(self, *, limit: int | None = None, search: str | None = None,
                 timeout: float = 20.0) -> list[OSSModel]:
        """Fetch concrete IDs and persist them; failure leaves prior inventory intact."""
        requested = max(self.target, int(limit or self.target))
        discovered = discover_huggingface_models(
            limit=requested, search=search, timeout=timeout
        )
        merged = oss_catalog(discovered=discovered)
        self.inventory = {item.model_id: item for item in merged if item.model_id}
        self._save()
        return list(self.inventory.values())

    def register(self, *, runtime_provider: str = "huggingface") -> int:
        """Register catalogued IDs without making them routable/live."""
        return register_oss_catalog(
            self.registry, self.inventory.values(), runtime_provider=runtime_provider
        )

    def verify(self, model_id: str,
               probe: Callable[[Model], RuntimeProbeResult]) -> RuntimeProbeResult:
        """Run a real adapter-supplied probe for one exact registered model."""
        if model_id not in self.inventory:
            raise KeyError(f"model is not in discovered inventory: {model_id}")
        return verify_model(self.registry, model_id, probe)

    def verify_many(self, model_ids: Iterable[str],
                    probe: Callable[[Model], RuntimeProbeResult]) -> list[RuntimeProbeResult]:
        results: list[RuntimeProbeResult] = []
        for model_id in model_ids:
            try:
                results.append(self.verify(model_id, probe))
            except Exception as exc:
                results.append(RuntimeProbeResult(
                    model_id=model_id,
                    ok=False,
                    latency_ms=0.0,
                    status="UNHEALTHY",
                    reason=str(exc),
                    capabilities=(),
                ))
        return results

    def snapshot(self) -> DiscoverySnapshot:
        registered = sum(1 for model_id in self.inventory if self.registry.has(model_id))
        verified = 0
        live = 0
        for model_id in self.inventory:
            if not self.registry.has(model_id):
                continue
            model = self.registry.get(model_id)
            if bool(model.metadata.get("runtime_verified")):
                verified += 1
            if bool(model.available):
                live += 1
        return DiscoverySnapshot(
            schema_version=1,
            discovered=len(self.inventory),
            registered=registered,
            verified=verified,
            live=live,
            target=self.target,
            source="huggingface-public-api",
        )

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "target": self.target,
            "source": "huggingface-public-api",
            "availability_policy": "catalogued-is-not-live",
            "entries": [asdict(item) for item in self.inventory.values()],
        }
        fd, temp_name = tempfile.mkstemp(prefix="real-models-", suffix=".tmp", dir=str(self.state_path.parent))
        try:
            with open(fd, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            Path(temp_name).replace(self.state_path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("real-model inventory entries must be a list")
        loaded: dict[str, OSSModel] = {}
        for raw in entries:
            if not isinstance(raw, dict) or not raw.get("model_id"):
                continue
            loaded[str(raw["model_id"])] = OSSModel(
                model_id=str(raw["model_id"]),
                provider=str(raw.get("provider", "huggingface")),
                source=str(raw.get("source", "huggingface-api")),
                status=str(raw.get("status", "discovered")),
                license=raw.get("license"),
                tags=tuple(str(x) for x in raw.get("tags", ()) if x),
            )
        self.inventory = loaded
