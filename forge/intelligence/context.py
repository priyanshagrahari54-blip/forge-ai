from __future__ import annotations

from dataclasses import dataclass, field


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

    def add(self, item: ContextItem) -> None:
        if item.path not in {existing.path for existing in self.items}:
            self.items.append(item)

    @property
    def files(self) -> list[str]:
        return [item.path for item in self.items]

    def sorted_items(self) -> list[ContextItem]:
        return sorted(
            self.items,
            key=lambda item: (-item.score, item.path, item.symbol or ""),
        )
