"""First-party Forge Agent Creation Engine.

:class:`AgentCreationEngine` is the factory for specialized Forge agents:
it turns validated :class:`AgentSpec` dictionaries into structured,
versioned :class:`AgentPackage` artifacts and walks them through the
seven-state lifecycle (created -> validated -> tested -> enabled ->
paused/disabled -> retired).

Security contract:

- Creating, validating, testing, enabling, or versioning an agent
  grants nothing — requested permissions become effective only through
  operator approval in the control plane.
- No agent may act on itself: every mutation requires an ``actor`` that
  is distinct from the agent's own identity. Self-grants,
  self-transitions, and self-updates are rejected with an explanation.
- Any spec change (including permission grants) bumps the version and
  resets lifecycle to ``created``, so validation and benchmarking must
  be re-earned.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from forge.agents import lifecycle
from forge.agents.package import (
    AgentPackage,
    bump_version,
    compare_versions,
    parse_version,
)
from forge.agents.spec import AgentSpec, PermissionGrant, validate_spec_dict

MAX_AGENTS = 40


def _require_operator(actor: str, agent_name: str, action: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise ValueError(f"{action} requires a named operator actor")
    lowered = actor.strip()
    if lowered == agent_name or lowered == f"forge-managed:{agent_name}" \
            or lowered.startswith("agent:"):
        raise ValueError(
            f"Agent {agent_name!r} cannot {action} itself: agents can "
            "never self-grant permissions or self-administer")
    return actor


class AgentCreationEngine:
    """Validated agent factory with lifecycle, versioning, and persistence."""

    def __init__(self, session_id: str = "",
                 store_dir: str | Path | None = None) -> None:
        self.session_id = session_id or ""
        self.store_dir = Path(store_dir) if store_dir else None
        self._packages: dict[str, AgentPackage] = {}
        if self.store_dir is not None:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            self._load_all()

    # -- persistence ---------------------------------------------------

    def _load_all(self) -> None:
        assert self.store_dir is not None
        for path in sorted(self.store_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Cannot load agent package {path.name}: {exc}") from exc
            try:
                package = AgentPackage.from_dict(data)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid agent package {path.name}: {exc}") from exc
            if len(self._packages) >= MAX_AGENTS:
                raise ValueError(f"Agent limit reached ({MAX_AGENTS})")
            self._packages[package.name] = package

    def _persist(self, package: AgentPackage) -> None:
        if self.store_dir is None:
            return
        path = self.store_dir / f"{package.name}.json"
        path.write_text(json.dumps(package.to_dict(), indent=2),
                        encoding="utf-8")

    def _forget(self, name: str) -> None:
        if self.store_dir is None:
            return
        path = self.store_dir / f"{name}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _record_transition(self, package: AgentPackage, current: str,
                           target: str, actor: str) -> None:
        trail = package.provenance.setdefault("transitions", [])
        trail.append({
            "from": current,
            "to": target,
            "actor": actor,
            "reason": lifecycle.reason_for(current, target),
            "at": time.time(),
        })
        package.provenance["transitions"] = trail[-50:]

    # -- reads ----------------------------------------------------------

    def get(self, name: str) -> AgentPackage | None:
        return self._packages.get((name or "").strip().lower())

    def list(self) -> list[AgentPackage]:
        return [self._packages[name] for name in sorted(self._packages)]

    def require(self, name: str) -> AgentPackage:
        package = self.get(name)
        if package is None:
            raise ValueError(f"Unknown agent: {(name or '').strip()}")
        return package

    # -- factory --------------------------------------------------------

    def create(self, spec_dict: dict[str, Any], actor: str,
               *, template: str = "") -> AgentPackage:
        if len(self._packages) >= MAX_AGENTS:
            raise ValueError(f"Agent limit reached ({MAX_AGENTS})")
        actor = (actor or "").strip()
        if not actor:
            raise ValueError("create requires a named operator actor")
        spec = validate_spec_dict(dict(spec_dict or {}))
        if spec.name in self._packages:
            raise ValueError(f"Agent already exists: {spec.name}")
        if actor == spec.name or actor == f"forge-managed:{spec.name}":
            raise ValueError(
                f"Agent {spec.name!r} cannot create itself")
        now = time.time()
        package = AgentPackage(
            name=spec.name, spec=spec, state=lifecycle.CREATED,
            version="1.0.0", created_by=actor,
            created_at=now, updated_at=now,
            provenance={"template": (template or "").strip().lower(),
                        "engine": "forge-creation-engine v1"})
        package.record_history(actor=actor, reason="created")
        self._packages[package.name] = package
        self._persist(package)
        return package

    def create_from_template(self, template: str, name: str, actor: str,
                             purpose: str = "") -> AgentPackage:
        from forge.agents.templates import build_spec

        spec_dict = build_spec(template, name, purpose)
        return self.create(spec_dict, actor, template=template)

    # -- spec updates + versioning ---------------------------------------

    def update(self, name: str, spec_dict: dict[str, Any], actor: str, *,
               bump: str = "patch", reason: str = "") -> AgentPackage:
        package = self.require(name)
        actor = _require_operator(actor, package.name, "update")
        if package.state == lifecycle.RETIRED:
            raise ValueError("Retired agents cannot be updated")
        merged = validate_spec_dict(dict(spec_dict or {}))
        if merged.name != package.name:
            raise ValueError("Spec name cannot change on update")
        package.record_history(actor=actor,
                               reason=f"pre-update {package.version}")
        new_version = bump_version(package.version, bump)
        package.spec = merged
        package.version = new_version
        package.updated_at = time.time()
        if package.state != lifecycle.CREATED:
            self._record_transition(package, package.state,
                                    lifecycle.CREATED, actor)
            package.state = lifecycle.CREATED
        package.record_history(
            actor=actor, reason=(reason or f"updated to {new_version}"))
        self._persist(package)
        return package

    def set_version(self, name: str, version: str, actor: str,
                    reason: str = "") -> AgentPackage:
        package = self.require(name)
        actor = _require_operator(actor, package.name, "re-version")
        if package.state == lifecycle.RETIRED:
            raise ValueError("Retired agents cannot be re-versioned")
        parse_version(version)
        if compare_versions(version, package.version) <= 0:
            raise ValueError(
                f"New version {version} must exceed {package.version}")
        package.record_history(actor=actor,
                               reason=f"pre-version {package.version}")
        package.version = version.strip()
        package.updated_at = time.time()
        if package.state != lifecycle.CREATED:
            self._record_transition(package, package.state,
                                    lifecycle.CREATED, actor)
            package.state = lifecycle.CREATED
        package.record_history(
            actor=actor, reason=(reason or f"versioned to {version}"))
        self._persist(package)
        return package

    def bump(self, name: str, actor: str, part: str = "patch",
             reason: str = "") -> AgentPackage:
        package = self.require(name)
        return self.set_version(
            name, bump_version(package.version, part), actor,
            reason or f"bumped {part}")

    # -- permissions (operator-only; never self-granted) ------------------

    def grant_permission(self, name: str, grant: dict[str, Any],
                         actor: str) -> AgentPackage:
        """Append one permission grant (operator only).

        The grant is validated, the version bumps (patch), and lifecycle
        resets to ``created`` — the agent must re-earn validation,
        testing, and enablement before the broader spec can run.
        """
        package = self.require(name)
        actor = _require_operator(actor, package.name, "grant permissions to")
        if package.state == lifecycle.RETIRED:
            raise ValueError("Retired agents cannot gain permissions")
        parsed = PermissionGrant.from_dict(dict(grant or {}))
        parsed.validate()
        if len(package.spec.permissions) >= 16:
            raise ValueError("Too many permissions (max 16)")
        for existing in package.spec.permissions:
            if (existing.resource, existing.operation, existing.scope) == \
                    (parsed.resource, parsed.operation, parsed.scope):
                raise ValueError("That permission is already granted")
        package.record_history(actor=actor,
                               reason=f"pre-grant {package.version}")
        package.spec = AgentSpec(
            name=package.spec.name,
            purpose=package.spec.purpose,
            capabilities=package.spec.capabilities,
            tools=package.spec.tools,
            permissions=tuple([*package.spec.permissions, parsed]),
            model_requirements=package.spec.model_requirements,
            memory_policy=package.spec.memory_policy,
            verification_requirements=package.spec.verification_requirements,
            resource_limits=package.spec.resource_limits,
        )
        package.version = bump_version(package.version, "patch")
        package.updated_at = time.time()
        if package.state != lifecycle.CREATED:
            self._record_transition(package, package.state,
                                    lifecycle.CREATED, actor)
            package.state = lifecycle.CREATED
        package.record_history(
            actor=actor,
            reason=(f"granted {parsed.resource}/{parsed.operation} "
                    f"scope={parsed.scope or '(default)'}"))
        self._persist(package)
        return package

    def request_permission(self, name: str, grant: dict[str, Any],
                           requested_by: str) -> dict[str, Any]:
        """File a permission *request* for operator decision.

        Agents call this — it never grants. The caller (control plane)
        files the returned payload as an approval request.
        """
        package = self.require(name)
        parsed = PermissionGrant.from_dict(dict(grant or {}))
        parsed.validate()
        return {
            "agent": package.name,
            "requested_by": (requested_by or "")[:64],
            "grant": parsed.to_dict(),
            "granted": False,
            "note": ("Requests never self-grant. An operator must approve "
                     "and apply this via grant_permission."),
        }

    # -- lifecycle --------------------------------------------------------

    def transition(self, name: str, target: str, actor: str) -> AgentPackage:
        package = self.require(name)
        actor = _require_operator(actor, package.name, "transition")
        target = (target or "").strip().lower()
        lifecycle.check_transition(package.state, target)
        current = package.state
        package.state = target
        package.updated_at = time.time()
        self._record_transition(package, current, target, actor)
        self._persist(package)
        return package

    def validate(self, name: str, actor: str) -> dict[str, Any]:
        package = self.require(name)
        actor = _require_operator(actor, package.name, "validate")
        package.spec.validate()
        if package.state == lifecycle.CREATED:
            self.transition(name, lifecycle.VALIDATED, actor)
            package = self.require(name)
        elif package.state != lifecycle.VALIDATED:
            raise ValueError(
                f"Agent {name} is {package.state}; only created agents "
                "can be validated")
        return {"agent": package.name, "state": package.state,
                "version": package.version, "valid": True}

    def test(self, name: str, actor: str,
             fabric: Any = None) -> dict[str, Any]:
        from forge.agents.agent_benchmark import run_agent_benchmark

        package = self.require(name)
        actor = _require_operator(actor, package.name, "benchmark")
        if package.state not in (lifecycle.VALIDATED, lifecycle.TESTED):
            raise ValueError(
                f"Agent {name} is {package.state}; only validated agents "
                "can be benchmark-tested")
        report = run_agent_benchmark(package, fabric)
        package.record_benchmark(report)
        if report["success"] and package.state == lifecycle.VALIDATED:
            self.transition(name, lifecycle.TESTED, actor)
            package = self.require(name)
            report = {**report, "state": package.state}
        else:
            self._persist(package)
        return report

    def enable(self, name: str, actor: str) -> AgentPackage:
        package = self.require(name)
        if package.state == lifecycle.TESTED:
            return self.transition(name, lifecycle.ENABLED, actor)
        if package.state in (lifecycle.PAUSED, lifecycle.DISABLED):
            return self.transition(name, lifecycle.ENABLED, actor)
        raise ValueError(
            f"Agent {name} is {package.state}; only tested, paused, or "
            "disabled agents can be enabled")

    def pause(self, name: str, actor: str) -> AgentPackage:
        return self.transition(name, lifecycle.PAUSED, actor)

    def disable(self, name: str, actor: str) -> AgentPackage:
        package = self.require(name)
        if package.state not in (lifecycle.ENABLED, lifecycle.PAUSED):
            raise ValueError(
                f"Agent {name} is {package.state}; only enabled or paused "
                "agents can be disabled")
        return self.transition(name, lifecycle.DISABLED, actor)

    def retire(self, name: str, actor: str) -> AgentPackage:
        return self.transition(name, lifecycle.RETIRED, actor)

    # -- execution --------------------------------------------------------

    def run(self, name: str, requirement: str, **kwargs: Any) -> dict[str, Any]:
        from forge.agents.managed_executor import execute_managed_agent

        package = self.require(name)
        return execute_managed_agent(package, requirement, **kwargs)
