"""The Agent Factory (A81): specifications become structured agent packages.

The factory is the only producer of agent versions. It:

- validates the specification (via :class:`AgentSpec`),
- derives the granted permission set and its digest,
- builds an immutable :class:`AgentManifest` (version 1, or N+1 for
  updates),
- writes the structured package into the :class:`AgentStore`:
  ``agent.json`` + ``spec.json`` + ``permissions.lock``.

The no-self-grant rule lives here: updating an agent to a permission set
that is not a subset of the current grant is an escalation and is
refused with :class:`PermissionEscalationError` unless a human operator
explicitly confirms it (``operator_confirmed=True``). Agents themselves
have no access to the factory — only operators (CLI, desktop) do.
"""
from __future__ import annotations

import json
from typing import Any

from forge.agent_engine.errors import (
    AgentNotFoundError,
    PermissionEscalationError,
    SpecError,
)
from forge.agent_engine.spec import AgentSpec
from forge.agent_engine.store import AgentManifest, AgentStore, _utc_now

#: Versions may only be advanced by one step from the latest version.
#: (Enforced by the store's immutability plus this rule: no skipping.)
_DEFAULT_CREATED_BY = "operator"


def _build_manifest(spec: AgentSpec, *, version: int, template: str,
                    parent_version: int, created_by: str, changelog: str,
                    operator_confirmed: bool) -> AgentManifest:
    return AgentManifest(
        id=spec.name,
        name=spec.name,
        version=version,
        lifecycle="created",
        spec=spec,
        permissions=spec.permissions,
        permission_digest=spec.permission_digest(),
        fingerprint=spec.fingerprint(),
        template=template,
        parent_version=parent_version,
        created_at=_utc_now(),
        created_by=(created_by or _DEFAULT_CREATED_BY),
        changelog=(changelog or "").strip(),
        operator_confirmed=operator_confirmed,
    )


class AgentFactory:
    """Generates structured, versioned agent packages from specifications."""

    def __init__(self, store: AgentStore | None = None) -> None:
        self.store = store if store is not None else AgentStore()

    # -- creation ----------------------------------------------------------

    def create(self, spec: AgentSpec, *, created_by: str = "",
               template: str = "") -> AgentManifest:
        """Create version 1 of a new agent from a validated specification."""
        if not isinstance(spec, AgentSpec):
            spec = AgentSpec.from_dict(spec)  # raises SpecError on issues
        spec.validate()  # fail closed even on hand-built instances
        if spec.name in self.store.list_agents():
            raise SpecError(f"agent already exists: {spec.name!r}")
        manifest = _build_manifest(
            spec, version=1, template=(template or "").strip(),
            parent_version=0, created_by=created_by, changelog="initial",
            operator_confirmed=False)
        self.store.save_version(manifest)
        self.store.set_current(manifest.name, manifest.version, "created")
        self.store.append_event(manifest.name, {
            "event": "created", "version": manifest.version,
            "created_by": manifest.created_by,
            "template": manifest.template,
            "permission_digest": manifest.permission_digest,
            "fingerprint": manifest.fingerprint,
        })
        return manifest

    def create_version(self, name: str, spec: AgentSpec, *,
                       created_by: str = "", changelog: str = "",
                       operator_confirmed: bool = False) -> AgentManifest:
        """Create version N+1 of an existing agent.

        Permission sets are monotone non-increasing by default: a new
        version may keep or shrink the granted permissions, but never
        grow them unless the operator explicitly confirms the escalation.
        Even then, the new set must pass full spec validation (every
        extra permission needs a tool or a rationale).
        """
        if not isinstance(spec, AgentSpec):
            spec = AgentSpec.from_dict(spec)
        spec.validate()
        if spec.name != name:
            raise SpecError(
                f"agent identity is immutable: cannot rename {name!r} "
                f"to {spec.name!r}")
        if name not in self.store.list_agents():
            raise AgentNotFoundError(f"no agent named {name!r}")
        latest_version, _lifecycle = self.store.current(name)
        existing = self.store.load_manifest(name, latest_version)

        new_permissions = set(spec.permissions)
        current_permissions = set(existing.permissions)
        if not new_permissions <= current_permissions:
            if not operator_confirmed:
                added = sorted(new_permissions - current_permissions)
                raise PermissionEscalationError(
                    f"agent {name!r} may not self-grant permissions; "
                    f"new version would add {', '.join(added)}. Re-run "
                    f"with explicit operator confirmation to escalate.")
            # Operator-confirmed escalation: still bounded by validation,
            # and recorded permanently in the manifest and history.
        manifest = _build_manifest(
            spec, version=latest_version + 1, template=existing.template,
            parent_version=latest_version, created_by=created_by,
            changelog=changelog, operator_confirmed=operator_confirmed)
        self.store.save_version(manifest)
        self.store.set_current(manifest.name, manifest.version, "created")
        escalation = sorted(new_permissions - current_permissions)
        self.store.append_event(manifest.name, {
            "event": "versioned", "version": manifest.version,
            "parent_version": manifest.parent_version,
            "created_by": manifest.created_by,
            "permission_digest": manifest.permission_digest,
            "escalated_permissions": escalation,
            "operator_confirmed": operator_confirmed,
        })
        return manifest

    def create_from_template(self, name: str, template: str, *,
                              purpose: str = "", created_by: str = "",
                              overrides: dict[str, Any] | None = None
                              ) -> AgentManifest:
        """Create a new agent from one of the built-in templates."""
        from forge.agent_engine.templates import build_template_spec

        spec = build_template_spec(template, name, purpose=purpose)
        if overrides:
            merged = {**spec.to_dict(), **overrides}
            merged["name"] = name
            spec = AgentSpec.from_dict(merged)
        return self.create(spec, created_by=created_by, template=template)

    # -- packages -----------------------------------------------------------

    def render_package(self, name: str, version: int | None = None
                       ) -> dict[str, str]:
        """Return the structured agent package as ``{path: content}``.

        The same layout the store persists on disk, rendered in memory
        for export and inspection: ``agent.json`` (manifest), ``spec.json``
        (specification), ``permissions.lock`` (granted set + digest).
        """
        manifest = self.store.load_manifest(name, version)
        directory = self.store._version_dir(name, manifest.version)
        package: dict[str, str] = {}
        for filename in ("agent.json", "spec.json", "permissions.lock"):
            path = directory / filename
            if path.exists():
                package[filename] = path.read_text(encoding="utf-8")
        benchmarks = self.store.load_benchmarks(name, manifest.version)
        if benchmarks:
            package["benchmarks.json"] = json.dumps(
                benchmarks, indent=2, sort_keys=True)
        return package

    def export_package(self, name: str, version: int | None = None
                       ) -> dict[str, Any]:
        """Render the package plus manifest metadata for CLI/desktop."""
        manifest = self.store.load_manifest(name, version)
        return {
            "agent": manifest.to_dict(),
            "files": self.render_package(name, manifest.version),
        }
