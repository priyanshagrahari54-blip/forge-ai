"""Uvicorn server entrypoint for the Forge cockpit (A34)."""
from __future__ import annotations

import os
from pathlib import Path

from forge.api.app import ApiConfig, create_app
from forge.control.control_plane import ControlConfig, ControlPlane


def build_plane(projects: dict[str, str] | None = None,
                db_path: str = "") -> ControlPlane:
    config = ControlConfig.from_env()
    resolved = dict(projects or {})
    if not resolved:
        root = Path.cwd()
        resolved = {root.name or "forge": str(root)}
    config.projects = resolved
    if db_path:
        config.db_path = db_path
    return ControlPlane(config)


def build_app(plane: ControlPlane | None = None,
              projects: dict[str, str] | None = None,
              db_path: str = ""):
    plane = plane or build_plane(projects, db_path)
    origins = [origin.strip()
               for origin in os.environ.get("FORGE_ALLOWED_ORIGINS", "")
               .split(",") if origin.strip()]
    api_config = ApiConfig(
        allowed_origins=origins,
        secure_cookies=os.environ.get("FORGE_SECURE_COOKIES",
                                      "0") == "1")
    return create_app(plane, api_config)


def run(host: str = "127.0.0.1", port: int = 8000,
        projects: dict[str, str] | None = None,
        db_path: str = "") -> None:  # pragma: no cover - thin runner
    import uvicorn

    app = build_app(projects=projects, db_path=db_path)
    print("Forge cockpit: local-development server")
    print("Auth mode: local-dev sessions (no passwords configured). "
          "Do not expose to untrusted networks.")
    uvicorn.run(app, host=host, port=port, log_level="info")
