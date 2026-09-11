"""Persistent agent-package store (Forge Agent Creation Engine).

Packages persist as one JSON document per agent inside a directory
(``<store>/<name>.json``). Names are validated before they touch the
filesystem, writes are atomic (temp file + rename), and oversized or
malformed documents fail closed instead of loading partially.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from forge.agents.package import MAX_PACKAGES, AgentPackage

MAX_DOCUMENT_BYTES = 256 * 1024


class AgentStore:
    """File-backed store for agent packages."""

    def __init__(self, path: str | Path = ".forge/agents") -> None:
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        from forge.agents.spec import _checked_name

        checked = _checked_name(name)
        candidate = (self.root / ("%s.json" % checked)).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(
                "Agent name escapes the store: %r" % (name,)) from None
        return candidate

    def save(self, package: AgentPackage) -> AgentPackage:
        if not isinstance(package, AgentPackage):
            raise ValueError("store saves AgentPackage documents only")
        if not self._path_for(package.name).exists():
            if len(self.list_names()) >= MAX_PACKAGES:
                raise ValueError(
                    "Agent limit reached (%d)" % MAX_PACKAGES)
        document = json.dumps(package.to_dict(), indent=2,
                              default=str)
        if len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("Agent document too large")
        target = self._path_for(package.name)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.root), prefix=".agent-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(document)
            os.replace(tmp_name, target)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return package

    def load(self, name: str) -> AgentPackage | None:
        path = self._path_for(name)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                "Cannot read agent %r: %s" % (name, exc)) from exc
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError("Agent document too large: %r" % (name,))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError(
                "Malformed agent document %r: %s" % (name, exc)) from exc
        return AgentPackage.from_dict(payload)

    def delete(self, name: str) -> bool:
        path = self._path_for(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_names(self) -> list[str]:
        names: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            stem = path.stem
            try:
                from forge.agents.spec import _checked_name

                names.append(_checked_name(stem))
            except ValueError:
                continue
        return names

    def list(self) -> list[AgentPackage]:
        packages: list[AgentPackage] = []
        for name in self.list_names():
            try:
                package = self.load(name)
            except ValueError:
                continue
            if package is not None:
                packages.append(package)
        return packages

    def snapshot(self) -> dict[str, Any]:
        return {"store": str(self.root),
                "agents": self.list_names(),
                "count": len(self.list_names()),
                "limit": MAX_PACKAGES}
