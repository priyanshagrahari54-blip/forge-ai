"""Production-friendly Forge Web entrypoint.

Serves the existing authenticated Forge Cockpit/City and keeps the ControlPlane
running independently of browser tabs. Persistent state is stored in the
configured SQLite database path; use a persistent disk/volume in deployment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn

from forge.api.app import ApiConfig, create_app
from forge.control.control_plane import ControlConfig, ControlPlane


def _projects() -> dict[str, str]:
    raw = os.environ.get("FORGE_PROJECTS_JSON", "").strip()
    if raw:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("FORGE_PROJECTS_JSON must be an object of project_id -> root")
        return {str(k): str(v) for k, v in value.items()}
    project_id = os.environ.get("FORGE_PROJECT_ID", "forge").strip() or "forge"
    root = os.environ.get("FORGE_PROJECT_ROOT", os.getcwd()).strip() or os.getcwd()
    return {project_id: str(Path(root).resolve())}


def build_app():
    secure = os.environ.get("FORGE_SECURE_COOKIES", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    config = ControlConfig.from_env(
        projects=_projects(),
        local_dev_mode=os.environ.get("FORGE_AUTH_MODE", "production") != "production",
    )
    plane = ControlPlane(config)
    app = create_app(
        plane,
        ApiConfig(
            allowed_origins=[x.strip() for x in os.environ.get("FORGE_ALLOWED_ORIGINS", "").split(",") if x.strip()],
            secure_cookies=secure,
        ),
    )
    try:
        providers = plane.fabric.providers.names()
        models = [str(item.get("name", "")) for item in plane.fabric.registry.snapshot() if isinstance(item, dict)]
        print("[forge-runtime] providers=" + ",".join(providers))
        print("[forge-runtime] registered_models=%d" % len(models))
        if models:
            print("[forge-runtime] model_ids=" + ",".join(models[:32]))
        for provider_name in providers:
            provider = plane.fabric.providers.get(provider_name)
            list_models = getattr(provider, "list_models", None)
            if callable(list_models):
                try:
                    discovered = [str(x) for x in (list_models() or []) if str(x)]
                    configured = [m for m in models if m.startswith(provider_name + "/")]
                    matches = any(
                        m.split("/", 1)[1] in discovered
                        for m in configured
                        if "/" in m
                    )
                    print("[forge-runtime] discovery provider=%s advertised=%s configured_model_seen=%s count=%d"
                          % (provider_name, bool(discovered), matches, len(discovered)))
                except Exception as exc:
                    print("[forge-runtime] discovery provider=%s error=%s"
                          % (provider_name, type(exc).__name__))
    except Exception as exc:
        print("[forge-runtime] diagnostics_error=%s" % type(exc).__name__)
    app.state.forge_plane = plane
    return app


app = build_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", os.environ.get("FORGE_PORT", "8300"))),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
    )
