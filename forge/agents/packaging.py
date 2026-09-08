"""Agent packaging (A56): portable, validated agent definitions.

Exports are plain JSON specifications (name, role, capabilities,
skills, description, generation, metrics) — never secrets, never
executors. Imports re-run the full factory validation and always
arrive **unbound**: a definition imported from elsewhere has no
executor here until someone binds one locally, so importing can
never smuggle execution power.
"""
from __future__ import annotations

import json
import time
from typing import Any

FORMAT_VERSION = 1
MAX_PAYLOAD = 64 * 1024

EXPORT_FIELDS = ("name", "role", "capabilities", "base_capabilities",
                 "skills", "description", "generation", "metrics",
                 "created_by")


def export_definition(definition: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": "forge-agent-definition",
        "format_version": FORMAT_VERSION,
        "exported_at": time.time(),
    }
    for field in EXPORT_FIELDS:
        value = getattr(definition, field, None)
        if isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        payload[field] = value
    return payload


def import_payload(payload: Any, *, skill_lookup=None) -> dict[str, Any]:
    """Validate an import payload; returns the checked fields.

    ``skill_lookup`` maps skill names to definitions in the target
    session; skills that do not exist locally are dropped honestly.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError(f"Malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("An import payload must be a JSON object")
    if payload.get("format") != "forge-agent-definition":
        raise ValueError("Not a forge-agent-definition payload")
    if payload.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported format_version "
            f"{payload.get('format_version')!r}")
    if len(json.dumps(payload, default=str)) > MAX_PAYLOAD:
        raise ValueError("Payload too large")
    name = payload.get("name", "")
    role = payload.get("role", "")
    capabilities = payload.get("capabilities") or []
    description = payload.get("description") or ""
    generation = payload.get("generation", 1)
    metrics = payload.get("metrics") or {}
    if not isinstance(name, str) or not isinstance(role, str):
        raise ValueError("name and role must be strings")
    if not isinstance(capabilities, list) or \
            not all(isinstance(cap, str) for cap in capabilities):
        raise ValueError("capabilities must be a list of strings")
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError("description must be <= 500 characters")
    if not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    skills = payload.get("skills") or []
    if not isinstance(skills, list) or \
            not all(isinstance(skill, str) for skill in skills):
        raise ValueError("skills must be a list of strings")
    kept_skills: list[str] = []
    dropped_skills: list[str] = []
    for skill in skills[:16]:
        if skill_lookup is None or skill_lookup(skill) is not None:
            kept_skills.append(skill)
        else:
            dropped_skills.append(skill)
    return {"name": name, "role": role,
            "capabilities": list(capabilities),
            "description": description, "generation": int(generation),
            "metrics": metrics, "skills": kept_skills,
            "dropped_skills": dropped_skills,
            "created_by": str(payload.get("created_by", ""))[:64]}
