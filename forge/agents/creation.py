"""Forge Agent Creation Engine: specs become lifecycle-gated packages.

The engine is the single first-party path for creating specialized agents:

* :meth:`AgentCreationEngine.create_from_spec` / ``create_from_template``
  validate a spec and emit a structured :class:`AgentPackage`.
* Packages move through an explicit lifecycle — ``created → validated →
  tested → enabled ⇄ paused``, with ``disabled`` and terminal ``retired`` —
  and only ``enabled`` packages may execute.
* Permissions listed in a spec are *requests*. Grants are recorded only via
  :meth:`AgentCreationEngine.grant_permission` with an approver identity
  distinct from the agent itself; self-grants are refused, always.
* Any change to what an agent may do (spec update, grant, revocation)
  resets the lifecycle to ``created`` and records a patch version, so
  broader power must always re-earn validation, testing, and enablement.
* Versions are explicit semver records with spec hashes and notes; history
  is append-only.
* Exports carry the spec and version only — never grants — so importing a
  package can never smuggle execution power.

Persistence is an atomic JSON store (``{"packages": {...}}``); invalid or
corrupt stores fail closed instead of loading partial state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from forge.agents.specs import AgentSpec, build_template, template_names

STORE_VERSION = 1
MAX_PACKAGES = 100
MAX_HISTORY = 50
MAX_BENCHMARKS = 20
MAX_NOTES = 280

STATES = ("created", "validated", "tested", "enabled", "paused",
          "disabled", "retired")

#: Allowed lifecycle transitions. Retired is terminal; nothing resurrects.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("validated", "retired"),
    "validated": ("tested", "disabled", "retired"),
    "tested": ("enabled", "disabled", "retired"),
    "enabled": ("paused", "disabled", "retired"),
    "paused": ("enabled", "disabled", "retired"),
    "disabled": ("enabled", "retired"),
    "retired": (),
}

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def agent_identity(name: str) -> str:
    """Canonical runtime identity for a created agent."""
    return "agent:%s" % name


def spec_fingerprint(spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def bump_version(version: str, kind: str) -> str:
    match = _VERSION_RE.match((version or "").strip())
    if match is None:
        raise ValueError("version %r is not semver" % (version,))
    major, minor, patch = (int(part) for part in match.groups())
    kind = (kind or "").strip().lower()
    if kind == "major":
        return "%d.0.0" % (major + 1,)
    if kind == "minor":
        return "%d.%d.0" % (major, minor + 1)
    if kind == "patch":
        return "%d.%d.%d" % (major, minor, patch + 1)
    raise ValueError("kind must be major, minor, or patch")


def _refuse_self_admin(name: str, actor: str, action: str) -> str:
    """Refuse administration by the agent itself (or any agent identity).

    Creating, validating, testing, moving, updating, versioning, or
    granting for an agent is operator work. Returns the stripped actor.
    """
    actor = (actor or "").strip()
    lowered = actor.strip()
    if lowered == name or lowered == agent_identity(name) \
            or lowered == "forge-managed:%s" % name \
            or lowered.lower().startswith("agent:"):
        raise ValueError(
            "refused: agents cannot %s themselves (%s); administration "
            "needs a distinct operator identity"
            % (action, actor or "anonymous"))
    return actor


@dataclass
class AgentPackage:
    """A structured, versioned, lifecycle-gated agent built from a spec."""

    name: str
    spec: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    state: str = "created"
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    grants: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    benchmarks: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def agent_spec(self) -> AgentSpec:
        return AgentSpec.from_dict(self.spec)

    def manifest(self) -> dict[str, Any]:
        """Stable package summary: identity, version, lifecycle, power."""
        spec = self.spec if isinstance(self.spec, dict) else {}
        return {
            "name": self.name,
            "version": self.version,
            "state": self.state,
            "identity": agent_identity(self.name),
            "role": spec.get("role", ""),
            "template": spec.get("template", ""),
            "capabilities": list(spec.get("capabilities", [])),
            "tools": list(spec.get("tools", [])),
            "grants": len(self.grants),
            "spec_hash": spec_fingerprint(spec),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "spec": self.spec,
            "version": self.version,
            "state": self.state,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "grants": list(self.grants),
            "versions": list(self.versions),
            "benchmarks": list(self.benchmarks),
            "transitions": list(self.transitions),
            "manifest": self.manifest(),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentPackage":
        if not isinstance(payload, dict):
            raise ValueError("an agent package must be an object")
        spec = payload.get("spec", {})
        # Stored specs re-validate: a corrupt store fails closed.
        AgentSpec.from_dict(spec).check()
        state = str(payload.get("state", ""))
        if state not in STATES:
            raise ValueError("unknown lifecycle state %r" % (state,))
        version = str(payload.get("version", ""))
        if _VERSION_RE.match(version) is None:
            raise ValueError("version %r is not semver" % (version,))
        package = cls(
            name=str(payload.get("name", "")),
            spec=spec,
            version=version,
            state=state,
            created_by=str(payload.get("created_by", "")),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
            grants=list(payload.get("grants", [])),
            versions=list(payload.get("versions", [])),
            benchmarks=list(payload.get("benchmarks", [])),
            transitions=list(payload.get("transitions", [])),
        )
        if package.name != package.spec.get("name"):
            raise ValueError("package name does not match its spec")
        return package


class AgentCreationEngine:
    """Factory + lifecycle + versioning for first-party agents."""

    def __init__(self, store_path: str = "") -> None:
        self.store_path = store_path
        self._packages: dict[str, AgentPackage] = {}
        if store_path:
            self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.store_path):
            return
        with open(self.store_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) \
                or payload.get("store_version") != STORE_VERSION \
                or not isinstance(payload.get("packages"), dict):
            raise ValueError("agent store %r is corrupt or unsupported"
                             % (self.store_path,))
        packages: dict[str, AgentPackage] = {}
        for name, entry in payload["packages"].items():
            package = AgentPackage.from_dict(entry)
            packages[package.name] = package
            del name
        self._packages = packages

    def _save(self) -> None:
        if not self.store_path:
            return
        directory = os.path.dirname(os.path.abspath(self.store_path))
        os.makedirs(directory, exist_ok=True)
        payload = {"store_version": STORE_VERSION,
                   "packages": {name: package.to_dict()
                                for name, package in self._packages.items()}}
        fd, tmp = tempfile.mkstemp(prefix=".agent-engine-",
                                   dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.store_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- factory ---------------------------------------------------------

    def templates(self) -> list[dict[str, Any]]:
        """First-party template summaries (never secrets, never power)."""
        from forge.agents.specs import TEMPLATES

        return [{"id": name,
                 "role": TEMPLATES[name].get("role", ""),
                 "purpose": TEMPLATES[name].get("purpose", ""),
                 "capabilities": list(
                     TEMPLATES[name].get("capabilities", [])),
                 "tools": list(TEMPLATES[name].get("tools", []))}
                for name in template_names()]

    def create_from_spec(self, payload: dict[str, Any], *,
                         created_by: str = "") -> AgentPackage:
        spec = AgentSpec.from_dict(payload).check()
        return self._store(spec, created_by=created_by)

    def create_from_template(self, template: str, name: str, *,
                             created_by: str = "",
                             overrides: dict[str, Any] | None = None,
                             ) -> AgentPackage:
        spec = build_template(template, name, overrides)
        return self._store(spec, created_by=created_by)

    def _store(self, spec: AgentSpec, *, created_by: str) -> AgentPackage:
        if len(self._packages) >= MAX_PACKAGES:
            raise ValueError("agent limit reached (%d)" % MAX_PACKAGES)
        if spec.name in self._packages:
            raise ValueError("agent already exists: %s" % spec.name)
        _refuse_self_admin(spec.name, created_by, "create")
        now = time.time()
        package = AgentPackage(
            name=spec.name, spec=spec.to_dict(), state="created",
            created_by=(created_by or "").strip()[:64],
            created_at=now, updated_at=now,
            transitions=[{"from": "", "to": "created", "at": now,
                          "actor": (created_by or "").strip()[:64]}],
            versions=[{"version": "1.0.0", "kind": "initial",
                       "notes": "created from %s"
                       % (spec.template or "spec",),
                       "actor": (created_by or "").strip()[:64],
                       "at": now,
                       "spec_hash": spec_fingerprint(spec.to_dict())}])
        self._packages[package.name] = package
        self._save()
        return package

    # -- accessors -------------------------------------------------------

    def get(self, name: str) -> AgentPackage:
        package = self._packages.get((name or "").strip().lower())
        if package is None:
            raise ValueError("unknown agent: %s" % (name,))
        return package

    def list(self) -> list[AgentPackage]:
        return [self._packages[name] for name in sorted(self._packages)]

    # -- lifecycle -------------------------------------------------------

    def _transition(self, package: AgentPackage, to_state: str,
                    actor: str) -> AgentPackage:
        _refuse_self_admin(package.name, actor, "transition")
        allowed = TRANSITIONS.get(package.state, ())
        if to_state not in allowed:
            raise ValueError(
                "agent %r cannot move %s -> %s (allowed: %s)"
                % (package.name, package.state, to_state,
                   ", ".join(allowed) or "none — retired is terminal"))
        previous = package.transitions[-1]["to"] if package.transitions \
            else package.state
        package.state = to_state
        package.updated_at = time.time()
        package.transitions.append(
            {"from": previous, "to": to_state, "at": package.updated_at,
             "actor": (actor or "").strip()[:64]})
        package.transitions = package.transitions[-MAX_HISTORY:]
        self._save()
        return package

    def _reset_to_created(self, package: AgentPackage, actor: str,
                          reason: str) -> None:
        """Return a changed package to ``created`` so broader power must
        re-earn validation, testing, and enablement."""
        if package.state != "created":
            package.transitions.append(
                {"from": package.state, "to": "created",
                 "at": time.time(),
                 "actor": (actor or "").strip()[:64]})
            package.transitions = package.transitions[-MAX_HISTORY:]
        package.state = "created"
        package.updated_at = time.time()
        self._record_release(package, kind="patch",
                             notes="spec power changed (%s); "
                             "re-validation required" % reason,
                             actor=actor)

    def _record_release(self, package: AgentPackage, *, kind: str,
                        notes: str, actor: str) -> dict[str, Any]:
        version = bump_version(package.version, kind)
        record = {"version": version, "kind": (kind or "").strip().lower(),
                  "notes": (notes or "").strip()[:MAX_NOTES],
                  "actor": (actor or "").strip()[:64],
                  "at": time.time(),
                  "spec_hash": spec_fingerprint(package.spec)}
        package.version = version
        package.versions.append(record)
        package.versions = package.versions[-MAX_HISTORY:]
        package.updated_at = time.time()
        return record

    def validate(self, name: str, *, actor: str = "") -> dict[str, Any]:
        """Re-validate the spec; created → validated on success."""
        package = self.get(name)
        _refuse_self_admin(package.name, actor, "validate")
        if package.state != "created":
            raise ValueError("agent %r is %s; only created agents validate"
                             % (name, package.state))
        issues = package.agent_spec().validate()
        report = {"agent": package.name, "valid": not issues,
                  "issues": issues, "at": time.time()}
        if issues:
            return report
        self._transition(package, "validated", actor or package.created_by)
        report["state"] = package.state
        return report

    def benchmark(self, name: str, *, actor: str = "",
                  fabric: Any = None) -> dict[str, Any]:
        """Run the benchmark suite; validated → tested when the score
        clears the spec's minimum. Re-running on tested/enabled/paused
        agents records a fresh result without moving state."""
        from forge.agents.agent_bench import run_agent_benchmark

        package = self.get(name)
        _refuse_self_admin(package.name, actor, "benchmark")
        if package.state not in ("validated", "tested", "enabled",
                                 "paused"):
            raise ValueError(
                "agent %r is %s; benchmark it after validation"
                % (name, package.state))
        report = run_agent_benchmark(package, fabric=fabric)
        package.benchmarks.append(report)
        package.benchmarks = package.benchmarks[-MAX_BENCHMARKS:]
        package.updated_at = time.time()
        if report["passed"] and package.state == "validated":
            self._transition(package, "tested", actor or package.created_by)
        else:
            self._save()
        report = dict(report)
        report["state"] = package.state
        return report

    def enable(self, name: str, *, actor: str = "") -> AgentPackage:
        package = self.get(name)
        if package.state == "tested" and not package.benchmarks:
            raise ValueError("agent %r was never benchmarked" % (name,))
        if package.state == "tested" and package.benchmarks \
                and not package.benchmarks[-1].get("passed"):
            raise ValueError("agent %r failed its latest benchmark"
                             % (name,))
        return self._transition(package, "enabled", actor)

    def pause(self, name: str, *, actor: str = "") -> AgentPackage:
        return self._transition(self.get(name), "paused", actor)

    def resume(self, name: str, *, actor: str = "") -> AgentPackage:
        package = self.get(name)
        if package.state != "paused":
            raise ValueError("agent %r is %s; only paused agents resume"
                             % (name, package.state))
        return self._transition(package, "enabled", actor)

    def disable(self, name: str, *, actor: str = "") -> AgentPackage:
        return self._transition(self.get(name), "disabled", actor)

    def retire(self, name: str, *, actor: str = "") -> AgentPackage:
        return self._transition(self.get(name), "retired", actor)

    # -- spec updates (always re-earn the lifecycle) ---------------------

    def update(self, name: str, payload: dict[str, Any], *,
               actor: str = "", reason: str = "") -> AgentPackage:
        """Replace the spec, bump patch, and reset to ``created``."""
        package = self.get(name)
        _refuse_self_admin(package.name, actor, "update")
        if package.state == "retired":
            raise ValueError("retired agents cannot be updated")
        spec = AgentSpec.from_dict(payload).check()
        if spec.name != package.name:
            raise ValueError("spec name cannot change on update")
        package.spec = spec.to_dict()
        self._reset_to_created(package, actor, reason or "spec update")
        self._save()
        return package

    # -- grants (never self-granted) --------------------------------------

    def grant_permission(self, name: str, index: int, *,
                         approver: str = "") -> dict[str, Any]:
        """Grant one requested permission. The approver must be a named
        identity distinct from the agent itself — self-grants raise.
        Grants widen power, so the lifecycle resets to ``created``."""
        package = self.get(name)
        approver = (approver or "").strip()
        if not approver:
            raise ValueError("grants need a named approver")
        if approver == agent_identity(package.name):
            raise ValueError(
                "refused: agents cannot grant themselves permissions "
                "(%s)" % approver)
        if package.state == "retired":
            raise ValueError("retired agents cannot gain permissions")
        requested = list(package.spec.get("permissions", []))
        if not isinstance(index, int) or not 0 <= index < len(requested):
            raise ValueError("permission index %r is out of range" % (index,))
        entry = dict(requested[index])
        if any(grant.get("permission") == entry for grant in package.grants):
            raise ValueError("permission is already granted")
        grant = {"permission": entry, "approver": approver,
                 "at": time.time()}
        package.grants.append(grant)
        self._reset_to_created(package, approver, "grant")
        self._save()
        return grant

    def revoke_permission(self, name: str, index: int, *,
                          approver: str = "") -> dict[str, Any]:
        """Revoke one grant. Like grants, revocations change effective
        power, so the lifecycle resets to ``created``."""
        package = self.get(name)
        approver = (approver or "").strip()
        if not approver:
            raise ValueError("revocations need a named approver")
        if approver == agent_identity(package.name):
            raise ValueError("refused: agents cannot change their own "
                             "permissions (%s)" % approver)
        if package.state == "retired":
            raise ValueError("retired agents cannot change permissions")
        if not isinstance(index, int) or not 0 <= index < len(package.grants):
            raise ValueError("grant index %r is out of range" % (index,))
        revoked = package.grants.pop(index)
        self._reset_to_created(package, approver, "revocation")
        self._save()
        return revoked

    # -- versioning --------------------------------------------------------

    def publish_version(self, name: str, *, kind: str = "patch",
                        notes: str = "", actor: str = "") -> dict[str, Any]:
        """Record a release marker. The spec is unchanged, so no reset is
        needed — the spec hash in the record proves it."""
        package = self.get(name)
        _refuse_self_admin(package.name, actor, "re-version")
        if package.state == "retired":
            raise ValueError("retired agents cannot publish versions")
        record = self._record_release(package, kind=kind, notes=notes,
                                      actor=actor)
        self._save()
        return record

    # -- portable packages (grants never travel) ---------------------------

    def export_package(self, name: str) -> dict[str, Any]:
        package = self.get(name)
        return {"format": "forge-agent-package", "format_version": 1,
                "exported_at": time.time(), "name": package.name,
                "version": package.version, "spec": package.spec,
                "versions": list(package.versions)}

    def import_package(self, payload: Any, *,
                       created_by: str = "") -> AgentPackage:
        if not isinstance(payload, dict):
            raise ValueError("an import payload must be an object")
        if payload.get("format") != "forge-agent-package":
            raise ValueError("not a forge-agent-package payload")
        if payload.get("format_version") != 1:
            raise ValueError("unsupported package format_version %r"
                             % (payload.get("format_version"),))
        spec = AgentSpec.from_dict(payload.get("spec")).check()
        # Imports always arrive ungranted: execution power is local and
        # can never be smuggled across a package boundary. The spec was
        # just validated, so the package starts validated.
        package = self._store(spec, created_by="import:%s" % (created_by or
                                                              "operator"))
        if package.state == "created":
            self._transition(package, "validated",
                             "import:%s" % (created_by or "operator"))
        return package
