"""Metadata-only package registry discovery for Forge.
No package is installed or executed by discovery.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from forge.capabilities.registry import CapabilityCandidate, ReuseStrategy


def _json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "Forge-AI-package-discovery/1.0"})
    with urlopen(request, timeout=10) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def discover_pypi(package: str) -> CapabilityCandidate | None:
    name = package.strip()
    if not name:
        return None
    payload = _json("https://pypi.org/pypi/" + quote(name, safe="") + "/json")
    info = payload.get("info", {}) if isinstance(payload, dict) else {}
    project = str(info.get("name", name)).strip()
    if not project:
        return None
    return CapabilityCandidate(
        capability="package", name=project, source="pypi",
        interface="python-package", license=str(info.get("license", "") or ""),
        version=str(info.get("version", "") or ""), security_status="unverified",
        verification_status="unverified", compatibility=["python", "pypi"],
        cost="free-to-download", strategy=ReuseStrategy.REUSE.value)


def discover_npm(package: str) -> CapabilityCandidate | None:
    name = package.strip()
    if not name:
        return None
    payload = _json("https://registry.npmjs.org/" + quote(name, safe="@/"))
    if not isinstance(payload, dict):
        return None
    license_value = payload.get("license", "")
    if isinstance(license_value, dict):
        license_value = license_value.get("type", "")
    project = str(payload.get("name", name)).strip()
    if not project:
        return None
    latest = str((payload.get("dist-tags") or {}).get("latest", "") or "")
    return CapabilityCandidate(
        capability="package", name=project, source="npm",
        interface="npm-package", license=str(license_value or ""),
        version=latest, security_status="unverified",
        verification_status="unverified", compatibility=["javascript", "npm"],
        cost="free-to-download", strategy=ReuseStrategy.REUSE.value)
