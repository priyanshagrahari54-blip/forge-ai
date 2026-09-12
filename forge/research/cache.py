"""Bounded research cache (memory + optional on-disk JSON).

Only *successful* source outcomes are cached; failures, policy denials
and timeouts are never cached so a transient error cannot poison later
queries. Entries expire after a TTL and the store is bounded in size.
The on-disk layer lives under ``.forge/research_cache/`` (runtime state,
never committed) and stores only serialized results — never raw
credentials or request headers.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.research.provenance import (
    Citation, Provenance, ResearchResult, STATE_SUCCESS, SourceOutcome,
)

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_MAX_ENTRIES = 256


def cache_key(source: str, query: str, extra: str = "") -> str:
    material = f"{source}\x00{query.strip().lower()}\x00{extra}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _result_to_json(result: ResearchResult) -> Dict[str, Any]:
    data = result.to_dict()
    data["citation"] = result.citation.to_dict()
    return data


def _result_from_json(data: Dict[str, Any]) -> ResearchResult:
    cit = data.get("citation") or {}
    return ResearchResult(
        source=str(data.get("source", "")),
        provenance=Provenance(str(data.get("provenance"))),
        title=str(data.get("title", "")),
        snippet=str(data.get("snippet", "")),
        citation=Citation(
            locator=str(cit.get("locator", "")),
            line=cit.get("line"),
            title=str(cit.get("title", "")),
            retrieved_at=str(cit.get("retrieved_at", "")),
            final_url=str(cit.get("final_url", "")),
            status=int(cit.get("status", 0) or 0)),
        score=float(data.get("score", 0.0)),
        kind=str(data.get("kind", "text")),
        matched_terms=list(data.get("matched_terms", [])),
        metadata=dict(data.get("metadata", {})),
    )


class ResearchCache:
    """LRU-ish TTL cache for source outcomes."""

    def __init__(self, directory: Optional[str | Path] = None, *,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 clock=time.time) -> None:
        self.directory = Path(directory) if directory else None
        self.ttl = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._memory: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        if self.directory is not None:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.directory = None

    # -- public -----------------------------------------------------------

    def get(self, key: str) -> Optional[SourceOutcome]:
        entry = self._memory.get(key)
        if entry is None and self.directory is not None:
            entry = self._read_disk(key)
            if entry is not None:
                self._memory[key] = entry
        if entry is None or self._expired(entry):
            if entry is not None:
                self._evict(key)
            self.misses += 1
            return None
        self.hits += 1
        outcome = SourceOutcome(
            source=entry["source"],
            provenance=Provenance(entry["provenance"]),
            state=STATE_SUCCESS,
            results=[_result_from_json(r) for r in entry.get("results", [])],
            from_cache=True)
        return outcome

    def put(self, key: str, outcome: SourceOutcome) -> bool:
        """Store a successful outcome. Returns False when not cacheable."""
        if outcome.state != STATE_SUCCESS or not outcome.results:
            return False
        entry = {
            "source": outcome.source,
            "provenance": outcome.provenance.value,
            "stored_at": float(self._clock()),
            "results": [_result_to_json(r) for r in outcome.results],
        }
        self._memory[key] = entry
        self._trim()
        if self.directory is not None:
            self._write_disk(key, entry)
        return True

    def clear(self) -> int:
        count = len(self._memory)
        self._memory.clear()
        if self.directory is not None:
            for path in self.directory.glob("*.json"):
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    pass
        return count

    def stats(self) -> Dict[str, Any]:
        return {
            "entries": len(self._memory),
            "hits": self.hits,
            "misses": self.misses,
            "ttl_seconds": self.ttl,
            "max_entries": self.max_entries,
            "directory": str(self.directory) if self.directory else "",
        }

    # -- internals --------------------------------------------------------

    def _expired(self, entry: Dict[str, Any]) -> bool:
        if self.ttl == 0:
            return True
        return (float(self._clock()) - float(entry.get("stored_at", 0))) > self.ttl

    def _evict(self, key: str) -> None:
        self._memory.pop(key, None)
        if self.directory is not None:
            try:
                (self.directory / f"{key}.json").unlink()
            except OSError:
                pass

    def _trim(self) -> None:
        while len(self._memory) > self.max_entries:
            oldest = min(self._memory.items(),
                         key=lambda kv: kv[1].get("stored_at", 0))[0]
            self._evict(oldest)

    def _disk_path(self, key: str) -> Path:
        safe = "".join(ch for ch in key if ch.isalnum())[:64]
        return self.directory / f"{safe}.json"  # type: ignore[operator]

    def _read_disk(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._disk_path(key)
        try:
            if path.stat().st_size > 2_000_000:
                return None
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "results" not in data:
            return None
        return data

    def _write_disk(self, key: str, entry: Dict[str, Any]) -> None:
        path = self._disk_path(key)
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(entry, handle)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def disk_entries(self) -> List[str]:
        if self.directory is None:
            return []
        return sorted(p.stem for p in self.directory.glob("*.json"))
