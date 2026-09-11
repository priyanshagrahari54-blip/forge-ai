"""Package persistence (A81): bounded, path-safe, atomic file store.

Packages live under ``<root>/.forge/agents/``::

    .forge/agents/index.json                  name -> {agent_id, version}
    .forge/agents/<name>/package.json         current manifest
    .forge/agents/<name>/versions/v<n>.json   immutable version snapshots
    .forge/agents/<name>/memory/…             agent-private memory

Names are validated by the spec regex (lowercase slug, no traversal),
writes are atomic (temp file + rename), and nothing outside the store
root is ever touched. The store never holds secrets — packages are
plain specifications.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from forge.agents.engine.package import AgentPackage

MAX_PACKAGES = 64
MAX_MANIFEST_BYTES = 512 * 1024


class PackageStoreError(ValueError):
    """Raised for store-level violations (name clashes, corruption)."""


class PackageStore:
    """File-backed agent package store rooted at ``<root>/.forge/agents``."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.base = self.root / ".forge" / "agents"

    # -- paths --------------------------------------------------------------

    def _agent_dir(self, name: str) -> Path:
        from forge.agents.engine.spec import NAME_PATTERN

        if not name or not NAME_PATTERN.match(name):
            raise PackageStoreError(f"Invalid agent name {name!r}")
        return self.base / name

    # -- primitives -----------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, indent=2, sort_keys=True)
        if len(data.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise PackageStoreError("Package manifest exceeds size bound")
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False,
            prefix=".tmp-", suffix=".json")
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, path)
        except Exception:
            handle.close()
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            raise PackageStoreError(
                f"Corrupt package file {path.name}: {exc}") from None
        if not isinstance(data, dict):
            raise PackageStoreError(
                f"Corrupt package file {path.name}: expected an object")
        return data

    # -- CRUD -------------------------------------------------------------------

    def index(self) -> dict[str, dict[str, Any]]:
        path = self.base / "index.json"
        if not path.exists():
            return {}
        return self._read_json(path)

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        self._atomic_write(self.base / "index.json", index)

    def names(self) -> list[str]:
        return sorted(self.index())

    def exists(self, name: str) -> bool:
        return name in self.index() and \
            self._agent_dir(name).joinpath("package.json").exists()

    def save(self, package: AgentPackage) -> None:
        name = package.name
        if len(self.names()) >= MAX_PACKAGES and name not in self.index():
            raise PackageStoreError(
                f"Package store is full ({MAX_PACKAGES} agents)")
        agent_dir = self._agent_dir(name)
        manifest = package.manifest()
        self._atomic_write(agent_dir / "package.json", manifest)
        self._atomic_write(
            agent_dir / "versions" / f"v{package.version}.json", manifest)
        index = self.index()
        index[name] = {"agent_id": package.agent_id,
                       "version": package.version,
                       "status": package.status,
                       "updated_at": package.updated_at}
        self._write_index(index)

    def load(self, name: str) -> AgentPackage:
        if name not in self.index():
            raise PackageStoreError(f"Unknown agent: {name}")
        path = self._agent_dir(name) / "package.json"
        if not path.exists():
            raise PackageStoreError(
                f"Agent {name!r} is indexed but its package file is missing")
        return AgentPackage.from_manifest(self._read_json(path))

    def load_version(self, name: str, version: int) -> AgentPackage:
        """Load an immutable snapshot of one specific version."""
        if name not in self.index():
            raise PackageStoreError(f"Unknown agent: {name}")
        path = self._agent_dir(name) / "versions" / f"v{int(version)}.json"
        if not path.exists():
            raise PackageStoreError(
                f"Agent {name!r} has no stored version {version}")
        return AgentPackage.from_manifest(self._read_json(path))

    def all(self) -> list[AgentPackage]:
        return [self.load(name) for name in self.names()]

    def delete(self, name: str) -> dict[str, Any]:
        """Remove a package (operator path; retired-only is enforced by
        the factory, not the store)."""
        import shutil

        if name not in self.index():
            raise PackageStoreError(f"Unknown agent: {name}")
        agent_dir = self._agent_dir(name)
        shutil.rmtree(agent_dir, ignore_errors=False)
        index = self.index()
        removed = index.pop(name)
        self._write_index(index)
        return removed
