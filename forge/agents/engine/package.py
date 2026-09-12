"""The structured agent package (A81).

An agent is not a row in a table and not a Python object in memory: it is
a directory of plain JSON under ``<project>/.forge/agents/<name>/`` that
any tool can read, diff, and review:

.. code-block:: text

    .forge/agents/<name>/
      agent.json           manifest: spec + version + lifecycle + provenance
      versions/1.0.0.json  immutable spec snapshot per version
      benchmarks/<id>.json benchmark reports (one file per run)
      grants.json          the operator permission ledger
      history.json         bounded run history

Writes are atomic (temp file + ``os.replace``), every file is size
bounded, and version files are never overwritten — a version record is
evidence. Paths are confined to the agent root: a name that would
traverse is refused before any filesystem call.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.agents.engine.errors import (
    AgentExistsError,
    AgentNotFoundError,
    AgentPackageError,
    AgentVersionError,
)
from forge.agents.engine.lifecycle import LifecycleRecord
from forge.agents.engine.spec import NAME_PATTERN, AgentSpec

PACKAGE_FORMAT = "forge-agent-package"
FORMAT_VERSION = 1
AGENTS_DIRNAME = ".forge/agents"
MANIFEST = "agent.json"
GRANTS_FILE = "grants.json"
HISTORY_FILE = "history.json"
VERSIONS_DIR = "versions"
BENCHMARKS_DIR = "benchmarks"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_HISTORY = 100


def _atomic_write(path: Path, payload: Any) -> None:
    """Write JSON atomically so a crash cannot leave a torn manifest."""
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise AgentPackageError(
            "Refusing to write %s: %d bytes exceeds the %d-byte bound"
            % (path.name, len(encoded), MAX_FILE_BYTES))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(str(temporary), str(path))


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is None:
            raise AgentPackageError("Missing package file: %s" % path.name)
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AgentPackageError(
            "Corrupt package file %s: %s" % (path.name, exc)) from None


@dataclass
class AgentPackage:
    """One agent's complete on-disk state, loaded into memory."""

    name: str
    directory: Path
    spec: AgentSpec
    version: str
    lifecycle: LifecycleRecord
    created_by: str
    created_at: float
    updated_at: float = 0.0
    run_count: int = 0
    last_run: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.lifecycle.state

    def to_manifest(self) -> dict:
        return {
            "format": PACKAGE_FORMAT,
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "version": self.version,
            "spec": self.spec.to_dict(),
            "lifecycle": self.lifecycle.to_dict(),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "fingerprint": self.spec.fingerprint(),
            "run_count": self.run_count,
            "last_run": dict(self.last_run),
        }

    def summary(self) -> dict:
        """Compact view for listings (CLI, desktop, API)."""
        return {
            "name": self.name,
            "state": self.state,
            "version": self.version,
            "role": self.spec.role,
            "template": self.spec.template,
            "purpose": self.spec.purpose,
            "capabilities": list(self.spec.capabilities),
            "tools": self.spec.tool_names(),
            "operations": list(self.spec.permissions.operations),
            "mode_ceiling": self.spec.permissions.mode_ceiling,
            "memory_scope": self.spec.memory.scope,
            "run_count": self.run_count,
            "created_by": self.created_by,
            "updated_at": self.updated_at or self.created_at,
            "fingerprint": self.spec.fingerprint(),
            "benchmark_passed": bool(
                self.lifecycle.benchmark.get("passed")),
        }


class PackageStore:
    """Filesystem-backed store of agent packages under one project root."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.agents_dir = self.root / AGENTS_DIRNAME

    # -- naming ----------------------------------------------------------

    def directory_for(self, name: str) -> Path:
        candidate = (name or "").strip()
        if not NAME_PATTERN.match(candidate):
            raise AgentPackageError(
                "Agent name must match [a-z][a-z0-9_-]{2,48}: %r" % name)
        directory = (self.agents_dir / candidate).resolve()
        if directory.parent != self.agents_dir.resolve():
            raise AgentPackageError(
                "Agent path escapes the agent store: %r" % name)
        return directory

    def exists(self, name: str) -> bool:
        try:
            return (self.directory_for(name) / MANIFEST).is_file()
        except AgentPackageError:
            return False

    def names(self) -> list:
        if not self.agents_dir.is_dir():
            return []
        return sorted(
            entry.name for entry in self.agents_dir.iterdir()
            if (entry / MANIFEST).is_file())

    # -- lifecycle of the package itself ---------------------------------

    def create(self, spec: AgentSpec, *, actor: str,
               version: str = "1.0.0") -> AgentPackage:
        directory = self.directory_for(spec.name)
        if (directory / MANIFEST).exists():
            raise AgentExistsError("Agent already exists: %s" % spec.name)
        now = time.time()
        package = AgentPackage(
            name=spec.name, directory=directory, spec=spec, version=version,
            lifecycle=LifecycleRecord(), created_by=actor or "",
            created_at=now, updated_at=now)
        directory.mkdir(parents=True, exist_ok=True)
        self.write_manifest(package)
        self.record_version(package, notes="initial version")
        self.write_grants(spec.name, {"grants": [], "revocations": []})
        return package

    def write_manifest(self, package: AgentPackage) -> None:
        package.updated_at = time.time()
        _atomic_write(package.directory / MANIFEST, package.to_manifest())

    def load(self, name: str) -> AgentPackage:
        directory = self.directory_for(name)
        manifest_path = directory / MANIFEST
        if not manifest_path.is_file():
            raise AgentNotFoundError("No such agent: %s" % name)
        payload = _read_json(manifest_path)
        if not isinstance(payload, dict) or \
                payload.get("format") != PACKAGE_FORMAT:
            raise AgentPackageError(
                "%s is not a Forge agent package" % manifest_path)
        if payload.get("format_version") != FORMAT_VERSION:
            raise AgentPackageError(
                "Unsupported agent package format_version: %r"
                % payload.get("format_version"))
        try:
            spec = AgentSpec.from_dict(payload.get("spec"))
            lifecycle = LifecycleRecord.from_dict(payload.get("lifecycle"))
        except (ValueError, KeyError) as exc:
            raise AgentPackageError(
                "Unreadable agent package %s: %s" % (name, exc)) from None
        return AgentPackage(
            name=str(payload.get("name", name)), directory=directory,
            spec=spec, version=str(payload.get("version", "1.0.0")),
            lifecycle=lifecycle,
            created_by=str(payload.get("created_by", "")),
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
            run_count=int(payload.get("run_count", 0)),
            last_run=dict(payload.get("last_run") or {}))

    def remove(self, name: str) -> None:
        """Delete a package. Used by tests and by explicit operator cleanup."""
        import shutil

        directory = self.directory_for(name)
        if not directory.is_dir():
            raise AgentNotFoundError("No such agent: %s" % name)
        shutil.rmtree(str(directory), ignore_errors=True)

    # -- versions --------------------------------------------------------

    def version_path(self, name: str, version: str) -> Path:
        directory = self.directory_for(name)
        if not version or "/" in version or "\\" in version or ".." in version:
            raise AgentVersionError("Invalid version label: %r" % version)
        return directory / VERSIONS_DIR / ("%s.json" % version)

    def record_version(self, package: AgentPackage, *, notes: str = "",
                       actor: str = "") -> dict:
        """Write an immutable snapshot of the current spec."""
        path = self.version_path(package.name, package.version)
        if path.exists():
            raise AgentVersionError(
                "Version %s of %s is already recorded and is immutable"
                % (package.version, package.name))
        record = {
            "name": package.name,
            "version": package.version,
            "recorded_at": time.time(),
            "recorded_by": actor or package.created_by,
            "notes": notes,
            "fingerprint": package.spec.fingerprint(),
            "spec": package.spec.to_dict(),
        }
        _atomic_write(path, record)
        return record

    def list_versions(self, name: str) -> list:
        directory = self.directory_for(name) / VERSIONS_DIR
        if not directory.is_dir():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            payload = _read_json(path, default={})
            records.append({
                "version": str(payload.get("version", path.stem)),
                "recorded_at": float(payload.get("recorded_at", 0.0)),
                "recorded_by": str(payload.get("recorded_by", "")),
                "notes": str(payload.get("notes", "")),
                "fingerprint": str(payload.get("fingerprint", "")),
            })
        return records

    def read_version(self, name: str, version: str) -> dict:
        path = self.version_path(name, version)
        if not path.is_file():
            raise AgentVersionError(
                "No version %s recorded for %s" % (version, name))
        return dict(_read_json(path))

    # -- grants ----------------------------------------------------------

    def read_grants(self, name: str) -> dict:
        path = self.directory_for(name) / GRANTS_FILE
        payload = _read_json(path, default={"grants": [], "revocations": []})
        return {"grants": list(payload.get("grants") or []),
                "revocations": list(payload.get("revocations") or [])}

    def write_grants(self, name: str, ledger: dict) -> None:
        _atomic_write(self.directory_for(name) / GRANTS_FILE, ledger)

    # -- benchmarks & history -------------------------------------------

    def record_benchmark(self, name: str, report: dict) -> dict:
        run_id = str(report.get("run_id") or "").strip()
        if not run_id or "/" in run_id or ".." in run_id:
            raise AgentPackageError("Benchmark report needs a run_id")
        path = (self.directory_for(name) / BENCHMARKS_DIR
                / ("%s.json" % run_id))
        if path.exists():
            raise AgentPackageError(
                "Benchmark %s is already recorded" % run_id)
        _atomic_write(path, report)
        return report

    def benchmarks(self, name: str, limit: int = 10) -> list:
        directory = self.directory_for(name) / BENCHMARKS_DIR
        if not directory.is_dir():
            return []
        reports = []
        for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
            reports.append(dict(_read_json(path, default={})))
        return reports

    def append_history(self, name: str, entry: dict) -> list:
        path = self.directory_for(name) / HISTORY_FILE
        payload = _read_json(path, default={"runs": []})
        runs = list(payload.get("runs") or [])
        runs.append(entry)
        runs = runs[-MAX_HISTORY:]
        _atomic_write(path, {"runs": runs})
        return runs

    def history(self, name: str, limit: int = 20) -> list:
        path = self.directory_for(name) / HISTORY_FILE
        payload = _read_json(path, default={"runs": []})
        return list(payload.get("runs") or [])[-limit:]
