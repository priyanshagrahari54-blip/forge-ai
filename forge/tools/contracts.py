"""Universal tool contract and fail-closed invocation boundary."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permissions: frozenset[str] = frozenset()
    risk: str = "low"
    version: str = ""
    verified: bool = False
    enabled: bool = True

@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name.strip(): raise ValueError("tool name is required")
        if not spec.verified: raise ValueError("unverified tools cannot be registered")
        self._tools[spec.name]=spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

class ToolInvoker:
    """Policy boundary; the callable remains outside the registry."""
    def __init__(self, registry: ToolRegistry):
        self.registry=registry

    def invoke(self, name: str, action: Callable[..., Any], *args: Any,
               approved_permissions: frozenset[str] = frozenset(), **kwargs: Any) -> Any:
        spec=self.registry.get(name)
        if spec is None or not spec.enabled:
            raise PermissionError(f"tool unavailable: {name}")
        missing=spec.permissions - approved_permissions
        if missing:
            raise PermissionError(f"missing tool permissions: {sorted(missing)}")
        return action(*args,**kwargs)
