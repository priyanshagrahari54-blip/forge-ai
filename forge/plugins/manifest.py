"""Plugin manifests (A66): strict validation, no code loading."""
from __future__ import annotations

import re
from typing import Any

from forge.models.capabilities import is_capability

NAME = re.compile(r"^[a-z][a-z0-9._-]{2,48}$")
ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
MAX_CAPABILITIES = 16
MAX_DESCRIPTION = 500
KINDS = ("executor", "provider", "tool")


def _valid_version(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(
        part.isdigit() and part for part in parts)


def validate_manifest(payload: Any) -> dict[str, Any]:
    """Validate a plugin manifest; returns the cleaned declaration.

    Raises ValueError with an honest message on any violation. This
    never imports or executes anything.
    """
    if not isinstance(payload, dict):
        raise ValueError("a plugin manifest must be a JSON object")
    if payload.get("format") != "forge-plugin-manifest":
        raise ValueError("not a forge-plugin-manifest")
    if payload.get("format_version") != 1:
        raise ValueError(
            f"unsupported manifest version {payload.get('format_version')!r}")
    name = str(payload.get("name", "")).strip().lower()
    version = str(payload.get("version", "")).strip()
    kind = str(payload.get("kind", "")).strip().lower()
    description = str(payload.get("description", "")).strip()
    capabilities = payload.get("capabilities") or []
    entrypoint = str(payload.get("entrypoint", "")).strip()
    if not NAME.match(name):
        raise ValueError("plugin names must match [a-z][a-z0-9._-]{2,48}")
    if not _valid_version(version):
        raise ValueError("plugin versions must look like 1.2.3")
    if kind not in KINDS:
        raise ValueError(
            f"plugin kind must be one of {sorted(KINDS)}")
    if not isinstance(capabilities, list) or \
            not all(isinstance(cap, str) for cap in capabilities):
        raise ValueError("capabilities must be a list of strings")
    if len(capabilities) > MAX_CAPABILITIES:
        raise ValueError(f"too many capabilities ({MAX_CAPABILITIES} max)")
    caps = tuple(dict.fromkeys(
        (cap or "").strip().lower() for cap in capabilities))
    if not caps:
        raise ValueError("at least one capability is required")
    if any(not is_capability(cap) for cap in caps):
        raise ValueError("capabilities must come from the canonical "
                         "vocabulary")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"description must be <= {MAX_DESCRIPTION} chars")
    if entrypoint and not ENTRYPOINT.match(entrypoint):
        raise ValueError(
            "entrypoint must be a plain dotted identifier (1-128 chars)")
    return {"name": name, "version": version, "kind": kind,
            "description": description, "capabilities": list(caps),
            "entrypoint": entrypoint}
