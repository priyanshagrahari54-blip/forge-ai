"""Plugin registry (A66): session-bounded declarative plugins."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.plugins.manifest import validate_manifest

MAX_PLUGINS = 16


@dataclass
class InstalledPlugin:
    plugin_id: str
    manifest: dict[str, Any]
    installed_by: str = ""
    installed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"plugin_id": self.plugin_id,
                **self.manifest,
                "installed_by": self.installed_by,
                "installed_at": self.installed_at}


def bind_capabilities(plugin: InstalledPlugin,
                      real_capabilities: Callable[[str], bool]
                      ) -> dict[str, Any]:
    """Report which declared capabilities are actually registered.

    ``real_capabilities(cap)`` answers whether the environment has a
    real, registered provider for a capability (agent executor or
    model). Declarations alone never make a capability real.
    """
    declared = plugin.manifest["capabilities"]
    real = [cap for cap in declared if real_capabilities(cap)]
    return {
        "plugin_id": plugin.plugin_id,
        "name": plugin.manifest["name"],
        "declared": declared,
        "real": real,
        "unbound": [cap for cap in declared if cap not in real],
        "honest": len(real) == len(declared),
    }


class PluginRegistry:
    """Validated, session-bounded plugin declarations."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._plugins: dict[str, InstalledPlugin] = {}

    def install(self, manifest: Any, installed_by: str = ""
                ) -> InstalledPlugin:
        cleaned = validate_manifest(manifest)
        if len(self._plugins) >= MAX_PLUGINS:
            raise ValueError(f"plugin limit reached ({MAX_PLUGINS})")
        plugin_id = uuid.uuid4().hex[:12]
        plugin = InstalledPlugin(plugin_id, cleaned,
                                 installed_by=installed_by)
        self._plugins[plugin_id] = plugin
        return plugin

    def get(self, plugin_id: str) -> InstalledPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError:
            raise ValueError(f"Unknown plugin: {plugin_id}") from None

    def remove(self, plugin_id: str) -> InstalledPlugin:
        plugin = self.get(plugin_id)
        del self._plugins[plugin_id]
        return plugin

    def list(self) -> list[InstalledPlugin]:
        return [self._plugins[plugin_id]
                for plugin_id in sorted(self._plugins)]
