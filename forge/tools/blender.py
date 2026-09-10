"""Agent-callable Blender tool: procedural 3D scenes rendered headlessly.

Thin wrapper over :mod:`forge.media.blender` with JSON-friendly results
so orchestration workers and operators share one entry point.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge.media.blender import (
    BlenderRunner,
    BlenderSpecError,
    build_scene,
    example_scene,
)


class BlenderTool:
    """Build scene specs from JSON and render them without a GUI."""

    def __init__(self, blender: str | None = None,
                 default_out_dir: str = ".") -> None:
        self.runner = BlenderRunner(blender)
        self.default_out_dir = default_out_dir

    def check(self) -> dict[str, Any]:
        """Report Blender availability (version or install guidance)."""
        return self.runner.check()

    def example(self) -> dict[str, Any]:
        """Return a starter scene spec as a JSON-shaped dict."""
        return example_scene()

    def validate(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Validate a scene spec without rendering."""
        try:
            scene = build_scene(spec)
        except BlenderSpecError as exc:
            return {"valid": False, "error": str(exc)}
        return {"valid": True, "name": scene.name,
                "animated": scene.animated,
                "objects": (len(scene.meshes) + len(scene.texts)
                            + len(scene.lights))}

    def render(self, spec: dict[str, Any], out_dir: str | None = None, *,
               timeout: float = 600.0) -> dict[str, Any]:
        """Render a scene spec dict; returns the render report."""
        try:
            scene = build_scene(spec)
        except BlenderSpecError as exc:
            return {"available": self.runner.available, "success": False,
                    "spec_name": "", "error": f"Invalid scene: {exc}"}
        result = self.runner.render(
            scene, out_dir or self.default_out_dir, timeout=timeout)
        return result.to_dict()

    def render_file(self, spec_path: str, out_dir: str | None = None, *,
                    timeout: float = 600.0) -> dict[str, Any]:
        """Render a scene from a JSON file on disk."""
        try:
            data = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        except OSError as exc:
            return {"available": self.runner.available, "success": False,
                    "spec_name": "", "error": f"Cannot read spec: {exc}"}
        except ValueError as exc:
            return {"available": self.runner.available, "success": False,
                    "spec_name": "", "error": f"Spec is not JSON: {exc}"}
        if not isinstance(data, dict):
            return {"available": self.runner.available, "success": False,
                    "spec_name": "",
                    "error": "Scene spec must be a JSON object"}
        return self.render(data, out_dir, timeout=timeout)
