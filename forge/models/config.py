"""Model Fabric configuration.

Configuration can come from environment variables or a declarative file. Secret
values never appear here; provider credentials stay in the ``CredentialStore``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from forge.models.policy import RoutingPolicy


@dataclass
class FabricConfig:
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"
    ollama_enabled: bool = True
    ollama_context_window: int = 8192
    local_enabled: bool = True
    openai_enabled: bool = False
    openai_model: str = "gpt-4o-mini"
    default_capability: str = "coding"
    telemetry_enabled: bool = True
    telemetry_path: str | None = None
    policy: RoutingPolicy = field(default_factory=RoutingPolicy)
    #: Extra models as ``Model.to_dict()``-compatible mappings.
    extra_models: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        self.policy.validate()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, env: Mapping[str, str] | None = None) -> "FabricConfig":
        data = dict(data or {})
        env = dict(os.environ if env is None else env)

        ollama_url = data.get("ollama_url") or env.get("OLLAMA_URL") or "http://127.0.0.1:11434"
        ollama_model = data.get("ollama_model") or env.get("OLLAMA_MODEL") or "llama3.2"

        config = cls(
            ollama_url=str(ollama_url),
            ollama_model=str(ollama_model),
            ollama_enabled=bool(data.get("ollama_enabled", True)),
            ollama_context_window=int(data.get("ollama_context_window", 8192)),
            local_enabled=bool(data.get("local_enabled", True)),
            openai_enabled=bool(data.get("openai_enabled", False)) or bool(env.get("OPENAI_API_KEY")),
            openai_model=str(data.get("openai_model") or env.get("OPENAI_MODEL") or "gpt-4o-mini"),
            default_capability=str(data.get("default_capability", "coding")),
            telemetry_enabled=bool(data.get("telemetry_enabled", True)),
            telemetry_path=data.get("telemetry_path"),
            policy=RoutingPolicy.from_dict(data.get("policy")),
            extra_models=[dict(model) for model in data.get("models", [])],
        )
        return config

    @classmethod
    def load(cls, path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> "FabricConfig":
        """Load configuration from a file if present, layered over env.

        Looks for ``.forge/models.yaml`` then ``.forge/models.json`` by default.
        A missing file is not an error: environment defaults apply.
        """
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path))
        else:
            candidates.extend((Path(".forge/models.yaml"), Path(".forge/models.json")))

        data: dict[str, Any] = {}
        for candidate in candidates:
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8")
            if candidate.suffix == ".json":
                data.update(json.loads(text))
            else:
                try:
                    import yaml  # type: ignore
                except ImportError:
                    continue
                loaded = yaml.safe_load(text) or {}
                if isinstance(loaded, dict):
                    data.update(loaded)
        return cls.from_dict(data, env=env)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ollama_url": self.ollama_url,
            "ollama_model": self.ollama_model,
            "ollama_enabled": self.ollama_enabled,
            "ollama_context_window": self.ollama_context_window,
            "local_enabled": self.local_enabled,
            "openai_enabled": self.openai_enabled,
            "openai_model": self.openai_model,
            "default_capability": self.default_capability,
            "telemetry_enabled": self.telemetry_enabled,
            "telemetry_path": self.telemetry_path,
            "policy": self.policy.to_dict(),
            "models": [dict(model) for model in self.extra_models],
        }
