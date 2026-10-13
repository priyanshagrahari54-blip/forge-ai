"""Read-only Hugging Face Hub discovery adapter.

Discovery returns metadata only: it never downloads weights, executes Space code,
or promotes a candidate. Promotion remains the responsibility of the verified
capability-adoption pipeline.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HubModel:
    model_id: str
    pipeline_tag: str = ""
    license: str = ""
    downloads: int = 0
    likes: int = 0
    last_modified: str = ""
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "pipeline_tag": self.pipeline_tag,
            "license": self.license,
            "downloads": self.downloads,
            "likes": self.likes,
            "last_modified": self.last_modified,
            "tags": list(self.tags),
            "status": "discovered_unverified",
        }


class HuggingFaceHubDiscovery:
    """Bounded, read-only search against the public Hub API."""

    API = "https://huggingface.co/api/models"

    def __init__(self, *, timeout: float = 10.0, max_results: int = 20) -> None:
        self.timeout = max(2.0, min(float(timeout), 30.0))
        self.max_results = max(1, min(int(max_results), 100))

    def search_models(self, query: str, *, task: str = "") -> list[HubModel]:
        query = (query or "").strip()
        if not query or len(query) > 200:
            raise ValueError("query must contain 1-200 characters")
        params: dict[str, str | int] = {
            "search": query,
            "limit": self.max_results,
            "sort": "downloads",
            "direction": "-1",
        }
        if task.strip():
            params["pipeline_tag"] = task.strip()[:80]
        url = self.API + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "ForgeAI-capability-discovery/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Hugging Face Hub HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Hugging Face Hub unavailable: {exc.reason}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("Hugging Face Hub response exceeded 2 MiB")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Hugging Face Hub returned invalid JSON") from exc
        if not isinstance(data, list):
            raise RuntimeError("Hugging Face Hub returned an unexpected response")
        models: list[HubModel] = []
        for item in data[:self.max_results]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            tags = item.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            models.append(HubModel(
                model_id=item["id"],
                pipeline_tag=str(item.get("pipeline_tag") or ""),
                license=str(item.get("license") or ""),
                downloads=int(item.get("downloads") or 0),
                likes=int(item.get("likes") or 0),
                last_modified=str(item.get("lastModified") or ""),
                tags=tuple(str(tag) for tag in tags[:100]),
            ))
        return models
