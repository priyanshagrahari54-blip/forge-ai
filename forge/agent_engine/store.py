"""Durable, versioned, immutable agent storage (A81).

Layout under the store root (default ``.forge/agents``)::

    <root>/<name>/current.json          # {version, lifecycle, updated_at}
    <root>/<name>/versions/v<N>/agent.json        # immutable manifest
    <root>/<name>/versions/v<N>/spec.json         # pretty specification
    <root>/<name>/versions/v<N>/permissions.lock  # granted set + digest
    <root>/<name>/versions/v<N>/benchmarks.json   # latest benchmark run
    <root>/<name>/history.jsonl         # append-only audit of every event

Guarantees:

- Version directories are written exactly once: re-saving an existing
  version is refused, so history can never be silently rewritten.
- Every path is confined to the store root; agent names are re-validated
  against the specification pattern on every access.
- ``permissions.lock`` carries the digest of the granted permission set,
  which the runtime re-derives to prove the lock was never tampered with.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.agent_engine.errors import AgentNotFoundError, SpecError
from forge.agent_engine.lifecycle import AgentLifecycle, parse_lifecycle
from forge.agent_engine.spec import AgentSpec, _NAME

_VERSION_PREFIX = "v"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AgentManifest:
    """One immutable version of an agent package."""

    id: str
    name: str
    version: int
    lifecycle: str
    spec: AgentSpec
    permissions: tuple[str, ...]
    permission_digest: str
    fingerprint: str
    template: str
    parent_version: int
    created_at: str
    created_by: str
    changelog: str
    operator_confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "lifecycle": self.lifecycle,
            "spec": self.spec.to_dict(),
            "permissions": list(self.permissions),
            "permission_digest": self.permission_digest,
            "fingerprint": self.fingerprint,
            "template": self.template,
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "changelog": self.changelog,
            "operator_confirmed": self.operator_confirmed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentManifest":
        return cls(
            id=str(payload.get("id") or payload.get("name", "")),
            name=str(payload.get("name", "")),
            version=int(payload.get("version", 1)),
            lifecycle=str(payload.get("lifecycle", "created")),
            spec=AgentSpec.from_dict(payload.get("spec") or {}),
            permissions=tuple(str(permission) for permission in
                              payload.get("permissions") or ()),
            permission_digest=str(payload.get("permission_digest", "")),
            fingerprint=str(payload.get("fingerprint", "")),
            template=str(payload.get("template", "")),
            parent_version=int(payload.get("parent_version", 0)),
            created_at=str(payload.get("created_at", "")),
            created_by=str(payload.get("created_by", "")),
            changelog=str(payload.get("changelog", "")),
            operator_confirmed=bool(payload.get("operator_confirmed",
                                                False)),
        )


class AgentStore:
    """Filesystem persistence for versioned agent packages."""

    def __init__(self, root: str | Path = ".forge/agents") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path confinement --------------------------------------------------

    def _agent_dir(self, name: str) -> Path:
        if not _NAME.match(name or ""):
            raise AgentNotFoundError(
                f"invalid agent name: {name!r}")
        candidate = (self.root / name).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise AgentNotFoundError(
                f"agent name escapes the store: {name!r}") from None
        return candidate

    def _version_dir(self, name: str, version: int) -> Path:
        if version < 1:
            raise AgentNotFoundError(f"invalid agent version: {version}")
        return self._agent_dir(name) / "versions" / f"{_VERSION_PREFIX}{version}"

    # -- writes ------------------------------------------------------------

    def save_version(self, manifest: AgentManifest) -> Path:
        """Persist a brand-new version; refuse to overwrite an existing one.

        Returns the version directory. Raises if the version already
        exists — versions are immutable once written.
        """
        directory = self._version_dir(manifest.name, manifest.version)
        if directory.exists():
            raise AgentNotFoundError(
                f"version {manifest.version} of {manifest.name!r} already "
                f"exists and is immutable")
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "agent.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8")
        (directory / "spec.json").write_text(
            json.dumps(manifest.spec.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8")
        (directory / "permissions.lock").write_text(
            json.dumps({
                "agent": manifest.name,
                "version": manifest.version,
                "permissions": list(manifest.permissions),
                "digest": manifest.permission_digest,
                "locked_at": manifest.created_at,
            }, indent=2, sort_keys=True),
            encoding="utf-8")
        return directory

    def set_current(self, name: str, version: int,
                    lifecycle: AgentLifecycle | str) -> None:
        state = lifecycle if isinstance(lifecycle, AgentLifecycle) \
            else parse_lifecycle(lifecycle)
        directory = self._agent_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "current.json").write_text(
            json.dumps({"version": version, "lifecycle": state.value,
                        "updated_at": _utc_now()}, indent=2, sort_keys=True),
            encoding="utf-8")

    def save_benchmarks(self, name: str, version: int,
                        results: dict[str, Any]) -> None:
        directory = self._version_dir(name, version)
        if not directory.exists():
            raise AgentNotFoundError(
                f"unknown agent version: {name} v{version}")
        (directory / "benchmarks.json").write_text(
            json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")

    def load_benchmarks(self, name: str,
                        version: int) -> dict[str, Any] | None:
        path = self._version_dir(name, version) / "benchmarks.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_event(self, name: str, event: dict[str, Any]) -> None:
        """Append one audit event to the agent's history (never rewrites)."""
        directory = self._agent_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        record = {"at": _utc_now(), **event}
        with (directory / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    # -- reads --------------------------------------------------------------

    def _read_current(self, name: str) -> dict[str, Any]:
        path = self._agent_dir(name) / "current.json"
        if not path.exists():
            raise AgentNotFoundError(f"no agent named {name!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentNotFoundError(
                f"corrupt agent index for {name!r}: {exc}") from exc

    def current(self, name: str) -> tuple[int, str]:
        payload = self._read_current(name)
        return int(payload["version"]), str(payload["lifecycle"])

    def load_manifest(self, name: str,
                      version: int | None = None) -> AgentManifest:
        if version is None:
            version, _lifecycle = self.current(name)
        path = self._version_dir(name, version) / "agent.json"
        if not path.exists():
            raise AgentNotFoundError(
                f"no agent version: {name} v{version}")
        try:
            return AgentManifest.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise AgentNotFoundError(
                f"corrupt agent package for {name} v{version}: {exc}"
            ) from exc

    def versions(self, name: str) -> list[int]:
        directory = self._agent_dir(name) / "versions"
        if not directory.exists():
            return []
        found: list[int] = []
        for path in directory.iterdir():
            if (path.is_dir() and path.name.startswith(_VERSION_PREFIX)
                    and path.name[len(_VERSION_PREFIX):].isdigit()):
                found.append(int(path.name[len(_VERSION_PREFIX):]))
        return sorted(found)

    def list_agents(self) -> list[str]:
        return sorted(
            path.name for path in self.root.iterdir()
            if path.is_dir() and _NAME.match(path.name)
            and (path / "current.json").exists()
        )

    def history(self, name: str) -> list[dict[str, Any]]:
        path = self._agent_dir(name) / "history.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def lifecycle_by_version(self, name: str) -> dict[int, str]:
        """Replay history events to the last recorded lifecycle per version."""
        result: dict[int, str] = {}
        for event in self.history(name):
            version = event.get("version")
            target = event.get("to")
            if isinstance(version, int) and target:
                result[version] = str(target)
        return result

    def verify_integrity(self, name: str,
                         version: int | None = None) -> tuple[bool, str]:
        """Confirm the stored package matches its own digests.

        Returns ``(ok, detail)``. Detects tampering with the spec, the
        manifest fingerprint, or the permissions lock.
        """
        try:
            manifest = self.load_manifest(name, version)
        except AgentNotFoundError as exc:
            return False, str(exc)
        directory = self._version_dir(name, manifest.version)
        lock_path = directory / "permissions.lock"
        if not lock_path.exists():
            return False, "permissions.lock is missing"
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"permissions.lock is corrupt: {exc}"
        if lock.get("permissions") != list(manifest.permissions):
            return False, "permissions.lock does not match the manifest"
        if lock.get("digest") != manifest.spec.permission_digest():
            return False, "permission digest mismatch (lock tampered?)"
        if manifest.fingerprint != manifest.spec.fingerprint():
            return False, "spec fingerprint does not match the manifest"
        if manifest.permission_digest != manifest.spec.permission_digest():
            return False, "manifest permission digest does not match the spec"
        # The on-disk spec.json must be the exact specification the
        # manifest records — a tampered spec.json is detected here.
        spec_path = directory / "spec.json"
        if not spec_path.exists():
            return False, "spec.json is missing"
        try:
            disk_spec = AgentSpec.from_dict(
                json.loads(spec_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            return False, f"spec.json is corrupt: {exc}"
        except SpecError as exc:
            return False, f"spec.json no longer validates: {exc}"
        if disk_spec.fingerprint() != manifest.spec.fingerprint():
            return False, "spec.json does not match the manifest"
        return True, "package integrity verified"
