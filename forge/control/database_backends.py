"""Provider-neutral database backend catalog.
The catalog describes optional integrations; it never provisions paid/remote
databases or claims that an external service is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DatabaseBackend:
    name: str
    protocol: str
    embedded: bool
    optional: bool
    notes: str


DEFAULT_DATABASE_BACKENDS: tuple[DatabaseBackend, ...] = (
    DatabaseBackend("sqlite", "sqlite", True, False, "Default embedded backend."),
    DatabaseBackend("postgresql", "postgresql", False, True, "Optional remote/managed backend."),
    DatabaseBackend("redis-compatible", "redis", False, True, "Optional cache/queue backend."),
)


class DatabaseBackendCatalog:
    def __init__(self, backends: Iterable[DatabaseBackend] = DEFAULT_DATABASE_BACKENDS):
        self._items = {item.name: item for item in backends}

    def get(self, name: str) -> DatabaseBackend | None:
        return self._items.get(name)

    def available(self, *, embedded_only: bool = False) -> list[DatabaseBackend]:
        items = list(self._items.values())
        if embedded_only:
            items = [item for item in items if item.embedded]
        return sorted(items, key=lambda item: (not item.embedded, item.name))

    def choose_default(self) -> DatabaseBackend:
        return self._items["sqlite"]
