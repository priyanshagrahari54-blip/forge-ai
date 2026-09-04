from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from forge.intelligence.context import ContextItem, ContextPack


@dataclass(frozen=True)
class ContextFingerprint:
    value: str


class DeterministicContextPack:
    """Create a stable representation of a ContextPack."""

    @staticmethod
    def sort_items(items: list[ContextItem]) -> list[ContextItem]:
        return sorted(
            items,
            key=lambda item: (
                -item.score,
                item.path,
                item.kind,
                item.symbol or "",
                item.start_line or 0,
                item.end_line or 0,
            ),
        )

    @classmethod
    def normalize(cls, pack: ContextPack) -> ContextPack:
        result = ContextPack(query=pack.query)

        seen: set[tuple] = set()

        for item in cls.sort_items(pack.items):
            key = (
                item.path,
                item.kind,
                item.symbol,
                item.start_line,
                item.end_line,
            )

            if key in seen:
                continue

            seen.add(key)
            result.add(item)

        return result

    @classmethod
    def serialize(cls, pack: ContextPack) -> str:
        normalized = cls.normalize(pack)

        data = {
            "query": {
                "task": normalized.query.task,
                "target_files": list(normalized.query.target_files),
                "target_symbols": list(normalized.query.target_symbols),
                "include_tests": normalized.query.include_tests,
                "include_dependencies": normalized.query.include_dependencies,
                "include_dependents": normalized.query.include_dependents,
                "max_files": normalized.query.max_files,
            },
            "items": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "symbol": item.symbol,
                    "reason": item.reason,
                    "score": item.score,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                }
                for item in normalized.items
            ],
        }

        return json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def fingerprint(cls, pack: ContextPack) -> ContextFingerprint:
        serialized = cls.serialize(pack)

        digest = hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest()

        return ContextFingerprint(value=digest)
