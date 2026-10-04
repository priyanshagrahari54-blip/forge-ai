"""Bounded, provenance-aware context assembly for Forge tasks."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterable

@dataclass(frozen=True)
class ContextItem:
    kind: str
    content: Any
    source: str
    confidence: float = 1.0
    priority: int = 0

@dataclass
class AssembledContext:
    project_id: str
    items: list[ContextItem] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "truncated": self.truncated,
                "items":[{"kind":x.kind,"content":x.content,"source":x.source,
                          "confidence":x.confidence,"priority":x.priority} for x in self.items]}

class ContextAssembler:
    """Combines heterogeneous context without allowing one source to dominate silently."""
    def __init__(self, max_items: int = 64, max_chars: int = 50000):
        if max_items < 1 or max_chars < 1: raise ValueError("context limits must be positive")
        self.max_items=max_items; self.max_chars=max_chars

    def assemble(self, project_id: str, *sources: Iterable[ContextItem]) -> AssembledContext:
        items=[]
        for source in sources:
            items.extend(source)
        clean=[x for x in items if x.source and 0.0 <= x.confidence <= 1.0]
        clean.sort(key=lambda x:(-x.priority,-x.confidence,x.kind,x.source))
        out=[]; chars=0; truncated=False
        for item in clean:
            size=len(str(item.content))
            if len(out) >= self.max_items or chars + size > self.max_chars:
                truncated=True; continue
            out.append(item); chars += size
        return AssembledContext(project_id,out,truncated)
