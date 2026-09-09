from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextQuery:
    """Describes the repository context an agent needs."""

    task: str
    target_files: tuple[str, ...] = ()
    target_symbols: tuple[str, ...] = ()
    include_tests: bool = True
    include_dependencies: bool = True
    include_dependents: bool = True
    max_files: int = 20


@dataclass(frozen=True)
class ContextItem:
    """A single piece of repository context."""

    path: str
    kind: str = "file"
    symbol: str | None = None
    reason: str = ""
    score: float = 0.0
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class ContextPack:
    """Deterministic repository context selected for an agent."""

    query: ContextQuery
    items: list[ContextItem] = field(default_factory=list)
    _paths: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # Maintain internal path set for O(1) duplicate checks in add()
        self._paths = {item.path for item in self.items}

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "items":
            super().__setattr__("_paths", {item.path for item in value})

    def add(self, item: ContextItem) -> None:
        # Fast O(1) check using internal path set instead of rebuilding set on every addition
        if item.path not in self._paths:
            self._paths.add(item.path)
            self.items.append(item)

    @property
    def files(self) -> list[str]:
        return [item.path for item in self.items]

    def sorted_items(self) -> list[ContextItem]:
        return sorted(
            self.items,
            key=lambda item: (-item.score, item.path, item.symbol or ""),
        )
