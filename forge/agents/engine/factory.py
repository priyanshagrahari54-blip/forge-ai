"""Agent creation factory (A81): specification in, package out.

The factory is the *only* path that creates or changes agent packages.
It validates every specification against the canonical vocabularies,
emits a structured :class:`AgentPackage`, drives the lifecycle, and
appends immutable versions.

**No agent may self-grant.** Every mutating factory call names an
actor; actors that are themselves agents (``agent:<name>`` or the
package's own name) are refused outright, audited as denials, and can
never create, update, transition, or approve anything. Permissions in
a spec are a *ceiling* the PolicyGate may still tighten at runtime —
they are never a grant.
"""
from __future__ import annotations

import time
from typing import Any

from forge.agents.engine.lifecycle import (
    LifecycleError,
    LifecycleState,
    transition,
)
from forge.agents.engine.package import AgentPackage
from forge.agents.engine.spec import AgentSpecification
from forge.agents.engine.store import PackageStore, PackageStoreError

#: Prefix marking an actor that is itself an agent at runtime.
AGENT_ACTOR_PREFIX = "agent:"


class EngineGuard:
    """The no-self-grant boundary for the creation engine."""

    @staticmethod
    def is_agent_actor(actor: str) -> bool:
        """True when the actor represents an agent, not an operator."""
        actor = (actor or "").strip()
        return actor.startswith(AGENT_ACTOR_PREFIX) or actor == "agent"

    @staticmethod
    def assert_operator(actor: str, *, what: str) -> str:
        """Refuse agent actors for package mutations (fail closed)."""
        actor = (actor or "").strip()
        if not actor:
            raise PermissionError(
                f"{what} requires a named operator actor")
        if EngineGuard.is_agent_actor(actor):
            raise PermissionError(
                f"Agents cannot {what} — no agent may grant or change "
                "its own permissions, lifecycle, or specification; "
                "an operator must do this")
        return actor[:64]

    @staticmethod
    def runtime_actor(name: str) -> str:
        """The actor string an agent carries when it runs."""
        return f"{AGENT_ACTOR_PREFIX}{(name or '').strip()}"


class AgentCreationFactory:
    """Creates and manages structured agent packages in one store."""

    def __init__(self, store: PackageStore | None = None,
                 root: str = ".") -> None:
        self.store = store or PackageStore(root)
        self.denials: list[dict[str, Any]] = []

    # -- audit trail of refused mutations -----------------------------------

    def _deny(self, actor: str, what: str, reason: str) -> None:
        self.denials.append({
            "actor": actor, "action": what, "reason": reason,
            "at": time.time()})
        self.denials = self.denials[-200:]

    def _operator(self, actor: str, what: str) -> str:
        try:
            return EngineGuard.assert_operator(actor, what=what)
        except PermissionError as exc:
            # Refused before anything existed — still recorded as a
            # denial so the audit trail shows the attempt.
            self._deny(actor or "", what, str(exc))
            raise

    def _guard_self_reference(self, actor: str, package: AgentPackage,
                              what: str) -> None:
        """An agent may never act on its own package (or any package)."""
        if EngineGuard.is_agent_actor(actor) or actor == package.name:
            self._deny(actor, what,
                       "agent actor attempted a package mutation")
            raise PermissionError(
                f"Agents cannot {what} — this is an operator-only action")

    # -- create ---------------------------------------------------------------

    def create(self, spec: AgentSpecification | dict[str, Any], *,
               created_by: str) -> AgentPackage:
        """Validate a specification and emit a fresh agent package.

        The new package starts in ``created``; nothing about it can run
        until it is validated, tested, and enabled.
        """
        created_by = self._operator(created_by, "create agents")
        # An actor whose name is exactly an existing agent's name is
        # indistinguishable from that agent: refuse (no self-grant, and
        # no creating agents from an agent-shaped identity).
        if self.store.exists(created_by):
            self._deny(created_by, "create agents",
                       "actor name matches an existing agent")
            raise PermissionError(
                f"Actors may not share a name with an agent "
                f"({created_by!r} exists); agents cannot create agents")
        if isinstance(spec, dict):
            spec = AgentSpecification.from_dict(spec)
        spec.validate()
        if self.store.exists(spec.name):
            raise PackageStoreError(
                f"An agent named {spec.name!r} already exists; update it "
                "instead of recreating it")
        package = AgentPackage.create(spec, created_by=created_by)
        self.store.save(package)
        return package

    # -- read -------------------------------------------------------------------

    def get(self, name: str) -> AgentPackage:
        return self.store.load(name)

    def get_version(self, name: str, version: int) -> AgentPackage:
        return self.store.load_version(name, version)

    def list(self) -> list[AgentPackage]:
        return self.store.all()

    def names(self) -> list[str]:
        return self.store.names()

    # -- validate (created → validated) -------------------------------------------

    def validate(self, name: str, *, actor: str) -> AgentPackage:
        actor = self._operator(actor, "validate agents")
        package = self.get(name)
        self._guard_self_reference(actor, package, "validate itself")
        problems: list[str] = []
        try:
            package.spec.validate()
        except ValueError as exc:
            problems.append(str(exc))
        if problems:
            raise ValueError(
                "Specification failed validation: " + "; ".join(problems))
        package.status = transition(package.status,
                                    LifecycleState.VALIDATED.value).value
        package.touch()
        self.store.save(package)
        return package

    # -- test (validated|disabled → tested) — called only with a passed benchmark

    def mark_tested(self, name: str, *, actor: str,
                    benchmark: dict[str, Any]) -> AgentPackage:
        """Record a passed benchmark and move to ``tested``.

        The benchmark itself is run and judged by
        :mod:`forge.agents.engine.benchmark`; the factory only records
        its honest verdict.
        """
        actor = self._operator(actor, "record agent test results")
        package = self.get(name)
        self._guard_self_reference(actor, package, "record its own test")
        if not benchmark.get("passed"):
            raise ValueError(
                "The benchmark did not pass; the agent cannot advance to "
                "tested")
        package.status = transition(package.status,
                                    LifecycleState.TESTED.value).value
        package.last_benchmark = dict(benchmark)
        package.touch()
        self.store.save(package)
        return package

    # -- operator transitions ---------------------------------------------------

    def _transition(self, name: str, target: LifecycleState, *,
                    actor: str) -> AgentPackage:
        actor = self._operator(actor, f"set agent state to {target.value}")
        package = self.get(name)
        self._guard_self_reference(
            actor, package, f"move itself to {target.value}")
        package.status = transition(package.status, target.value).value
        package.touch()
        self.store.save(package)
        return package

    def enable(self, name: str, *, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.ENABLED, actor=actor)

    def pause(self, name: str, *, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.PAUSED, actor=actor)

    def resume(self, name: str, *, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.ENABLED, actor=actor)

    def disable(self, name: str, *, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.DISABLED, actor=actor)

    def retire(self, name: str, *, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.RETIRED, actor=actor)

    # -- versioning ----------------------------------------------------------------

    def update(self, name: str, spec: AgentSpecification | dict[str, Any],
               *, actor: str, note: str = "") -> AgentPackage:
        """Apply a new specification as a new version.

        The lifecycle resets to ``created``: a changed agent never
        inherits its old clearance — it must be re-validated,
        re-tested, and re-enabled before it can run again.
        """
        actor = self._operator(actor, "update agents")
        package = self.get(name)
        self._guard_self_reference(actor, package, "update itself")
        if isinstance(spec, dict):
            spec = AgentSpecification.from_dict(spec)
        spec.validate()
        if spec.name != name:
            raise ValueError(
                f"Update payload is for {spec.name!r}, not {name!r}; names "
                "are immutable — retire and create a new agent instead")
        if package.status == LifecycleState.RETIRED.value:
            raise LifecycleError(
                "Retired agents cannot be updated; create a new agent")
        new_version = package.history.current_version() + 1
        package.spec = spec
        package.version = new_version
        package.history.append(
            spec.to_dict(), changed_by=actor,
            note=note or spec.version_note or f"updated to v{new_version}")
        package.status = LifecycleState.CREATED.value
        package.last_benchmark = {}
        package.touch()
        self.store.save(package)
        return package

    def rollback(self, name: str, version: int, *, actor: str,
                 note: str = "") -> AgentPackage:
        """Re-promote an older spec version as a NEW version.

        History stays append-only; rollback is a forward change that
        copies an old snapshot, and the agent still re-enters the full
        lifecycle (created → validated → tested → enabled).
        """
        actor = self._operator(actor, "roll back agents")
        package = self.get(name)
        self._guard_self_reference(actor, package, "roll itself back")
        old = self.store.load_version(name, version)
        old_spec = old.spec
        if old_spec.name != name:  # defensive; names are immutable
            raise ValueError("Version snapshot name mismatch")
        return self.update(
            name, old_spec, actor=actor,
            note=note or f"rolled back to v{version}")

    # -- delete ---------------------------------------------------------------------

    def delete(self, name: str, *, actor: str) -> dict[str, Any]:
        """Remove a package entirely. Only retired packages may go."""
        actor = self._operator(actor, "delete agents")
        package = self.get(name)
        self._guard_self_reference(actor, package, "delete itself")
        if package.status != LifecycleState.RETIRED.value:
            raise LifecycleError(
                "Only retired agents can be deleted; retire first")
        return self.store.delete(name)
