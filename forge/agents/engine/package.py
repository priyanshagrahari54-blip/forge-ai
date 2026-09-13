"""The structured agent package (A82).

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
import shutil
import tempfile
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
    """Write JSON atomically so a crash cannot leave a torn manifest.

    The temporary name is unique per write, so two writers racing on the
    same manifest cannot overwrite each other's temp file, and the data is
    flushed to disk before the rename — a rename alone can survive a power
    loss pointing at an empty file.
    """
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise AgentPackageError(
            "Refusing to write %s: %d bytes exceeds the %d-byte bound"
            % (path.name, len(encoded), MAX_FILE_BYTES))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        # Never leave a temp file behind when the write did not land.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _exclusive_create(path: Path, payload: Any) -> None:
    """Create ``path`` only if it does not exist, atomically.

    This is the create side of the same guarantee :func:`_atomic_write`
    gives the update side: two callers cannot both succeed.
    """
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise AgentPackageError(
            "Refusing to write %s: %d bytes exceeds the %d-byte bound"
            % (path.name, len(encoded), MAX_FILE_BYTES))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
    except FileExistsError as exc:
        raise AgentExistsError(
            "Agent already exists: %s" % path.parent.name) from exc
    with os.fdopen(handle, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path, default: Any = None) -> Any:
    """Read one package file, refusing anything oversized or unreadable.

    The write path bounds file size; the read path must bound it too, or a
    hand-edited (or merely runaway) history file is parsed into memory in
    full. ``default=None`` means "missing is an error".
    """
    if not path.exists():
        if default is None:
            raise AgentPackageError("Missing package file: %s" % path.name)
        return default
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentPackageError(
            "Unreadable package file %s: %s" % (path.name, exc)) from None
    if size > MAX_FILE_BYTES:
        raise AgentPackageError(
            "Refusing to read %s: %d bytes exceeds the %d-byte bound"
            % (path.name, size, MAX_FILE_BYTES))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # ValueError covers json errors and UnicodeDecodeError alike.
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
    #: The fingerprint recorded in the manifest when it was written. The
    #: spec is the authority on what this agent may do, so a manifest whose
    #: spec no longer matches its own recorded fingerprint has been edited
    #: outside the factory and must not be trusted at run time.
    recorded_fingerprint: str = ""

    @property
    def state(self) -> str:
        return self.lifecycle.state

    def integrity_error(self) -> str:
        """Why this package cannot be trusted, or ``""`` when it can."""
        expected = (self.recorded_fingerprint or "").strip()
        if not expected:
            return ""
        actual = self.spec.fingerprint()
        if actual != expected:
            return ("agent package %r does not match its recorded "
                    "fingerprint (recorded %s, spec now %s); the "
                    "specification was edited outside the factory"
                    % (self.name, expected[:12], actual[:12]))
        return ""

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
        """Create a package. Atomic: it fully exists or not at all.

        The manifest is opened with ``O_EXCL``, so two concurrent creates
        cannot both pass an ``exists()`` check and silently clobber each
        other. If any later step fails the half-built directory is removed
        rather than left as an agent with no grants ledger.
        """
        directory = self.directory_for(spec.name)
        manifest = directory / MANIFEST
        now = time.time()
        package = AgentPackage(
            name=spec.name, directory=directory, spec=spec, version=version,
            lifecycle=LifecycleRecord(), created_by=actor or "",
            created_at=now, updated_at=now)
        directory.mkdir(parents=True, exist_ok=True)
        _exclusive_create(manifest, package.to_manifest())
        try:
            self.record_version(package, notes="initial version")
            self.write_grants(spec.name, {"grants": [], "revocations": []})
        except BaseException:
            self._discard(directory)
            raise
        return package

    @staticmethod
    def _discard(directory: Path) -> None:
        """Remove a half-created package directory."""
        shutil.rmtree(str(directory), ignore_errors=True)

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
            last_run=dict(payload.get("last_run") or {}),
            recorded_fingerprint=str(payload.get("fingerprint", "") or ""))

    def remove(self, name: str) -> None:
        """Delete a package. Used by tests and by explicit operator cleanup.

        A delete that fails part way must not report success: a leftover
        directory is an agent that still exists, still has grants, and can
        still be enabled.
        """
        directory = self.directory_for(name)
        if not directory.is_dir():
            raise AgentNotFoundError("No such agent: %s" % name)
        shutil.rmtree(str(directory))
        if directory.exists():
            raise AgentPackageError(
                "Could not fully remove %s; the agent still exists" % name)

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
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            raise AgentPackageError("Benchmark report needs a run_id")
        path = (self.directory_for(name) / BENCHMARKS_DIR
                / ("%s.json" % run_id))
        if path.exists():
            raise AgentPackageError(
                "Benchmark %s is already recorded" % run_id)
        _atomic_write(path, report)
        return report

    def benchmarks(self, name: str, limit: int = 10) -> list:
        """Most recent reports first.

        Run ids are random hex, so sorting by filename yields an arbitrary
        subset rather than the latest runs — the one thing a caller asking
        for "the last few benchmarks" wants.
        """
        directory = self.directory_for(name) / BENCHMARKS_DIR
        if not directory.is_dir() or limit <= 0:
            return []
        reports = [dict(_read_json(path, default={}))
                   for path in directory.glob("*.json")]
        reports.sort(key=lambda report: (
            float(report.get("recorded_at") or 0.0),
            str(report.get("run_id") or "")), reverse=True)
        return reports[:limit]

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
        runs = list(payload.get("runs") or [])
        # ``runs[-0:]`` is the whole list, so a zero or negative limit has
        # to be handled explicitly or it silently means "everything".
        if limit <= 0:
            return []
        return runs[-limit:]
