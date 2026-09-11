"""The Agent Manager (A81): the only lifecycle authority.

The manager owns the lifecycle state machine and every transition:

- ``create``     factory → version 1, then validate → ``validated``
- ``validate``   re-check spec + package integrity → ``validated``
- ``test``       run the spec's benchmark → ``tested`` (or back to
                 ``validated`` when requirements are not met)
- ``enable``     ``tested`` → ``enabled`` (only a fully tested version
                 may run)
- ``pause``      ``enabled`` → ``paused`` (resumable)
- ``resume``     ``paused`` → ``enabled``
- ``disable``    ``enabled``/``paused`` → ``disabled`` (re-entry requires
                 validate → test → enable again)
- ``retire``     terminal; nothing may ever run again

Agents never receive a reference to the manager (or the factory or the
store): they hold only an :class:`AgentRuntime` whose ``enabled`` flag
is decided here, from the store's recorded lifecycle. That is the
mechanical guarantee behind "no agent may self-grant permissions" and
"no agent may self-enable": the API surface an agent can touch contains
no transition method.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from forge.agent_engine.benchmarks import DEFAULT_HARNESS
from forge.agent_engine.errors import (
    AgentNotFoundError,
    LifecycleError,
    NotRunnableError,
    SpecError,
)
from forge.agent_engine.factory import AgentFactory
from forge.agent_engine.lifecycle import (
    AgentLifecycle,
    can_transition,
    is_runnable,
    parse_lifecycle,
)
from forge.agent_engine.runtime import AgentRuntime
from forge.agent_engine.spec import AgentSpec
from forge.agent_engine.store import AgentManifest, AgentStore
from forge.agent_engine.templates import template_catalog


class AgentManager:
    """Lifecycle authority over every created agent."""

    def __init__(self, root: str | Path = ".forge/agents",
                 workspace: str | Path = ".", fabric: Any = None) -> None:
        self.store = AgentStore(root)
        self.workspace = Path(workspace)
        self.factory = AgentFactory(self.store)
        self.fabric = fabric

    # -- helpers -------------------------------------------------------------

    def _require_agent(self, name: str) -> AgentManifest:
        if name not in self.store.list_agents():
            raise AgentNotFoundError(f"no agent named {name!r}")
        return self.store.load_manifest(name)

    def _live_state(self, name: str,
                    version: int | None = None) -> AgentLifecycle:
        """The authoritative lifecycle for a version.

        The current version's state lives in ``current.json``; past
        versions' states are replayed from the append-only history (the
        manifest's own lifecycle field is write-time only).
        """
        current_version, current_lifecycle = self.store.current(name)
        if version is None or version == current_version:
            return parse_lifecycle(current_lifecycle)
        derived = self.store.lifecycle_by_version(name)
        if version in derived:
            return parse_lifecycle(derived[version])
        return parse_lifecycle(self.store.load_manifest(name, version).lifecycle)

    def _transition(self, name: str, target: AgentLifecycle,
                    *, event: str = "transition", detail: str = "",
                    version: int | None = None) -> dict[str, Any]:
        manifest = self._require_agent(name)
        if version is None:
            version = manifest.version
        current = self._live_state(name, version)
        if not can_transition(current, target):
            raise LifecycleError(
                f"agent {name!r} cannot move {current.value} -> "
                f"{target.value}")
        # Only the latest version may become current.
        latest = self.store.versions(name)[-1] if self.store.versions(name) else 0
        if target == AgentLifecycle.ENABLED and version != latest:
            raise LifecycleError(
                f"only the latest version (v{latest}) may be enabled; "
                f"v{version} is superseded")
        self.store.set_current(name, version, target)
        self.store.append_event(name, {
            "event": event, "version": version,
            "from": current.value, "to": target.value, "detail": detail,
        })
        return self.show(name, version=version)

    # -- creation ------------------------------------------------------------

    def create(self, name: str, *, template: str = "", spec: AgentSpec
               | dict | None = None, purpose: str = "",
               created_by: str = "", overrides: dict | None = None
               ) -> dict[str, Any]:
        """Create a new agent and validate it (``created`` → ``validated``)."""
        if spec is not None:
            if not isinstance(spec, AgentSpec):
                spec = AgentSpec.from_dict(spec)
            manifest = self.factory.create(spec, created_by=created_by,
                                           template=template)
        else:
            manifest = self.factory.create_from_template(
                name, template, purpose=purpose, created_by=created_by,
                overrides=overrides)
        self._transition(name, AgentLifecycle.VALIDATED, event="validated",
                         version=manifest.version)
        return self.show(name)

    def create_version(self, name: str, spec: AgentSpec | dict, *,
                       created_by: str = "", changelog: str = "",
                       operator_confirmed: bool = False) -> dict[str, Any]:
        """Publish version N+1 (new versions start back at ``created``)."""
        if not isinstance(spec, AgentSpec):
            spec = AgentSpec.from_dict(spec)
        manifest = self.factory.create_version(
            name, spec, created_by=created_by, changelog=changelog,
            operator_confirmed=operator_confirmed)
        self._transition(name, AgentLifecycle.VALIDATED, event="validated",
                         version=manifest.version)
        return self.show(name, version=manifest.version)

    # -- lifecycle operations -------------------------------------------------

    def validate(self, name: str, *, version: int | None = None
                 ) -> dict[str, Any]:
        """Re-validate a version: spec validity plus package integrity."""
        manifest = self._require_agent(name)
        if version is None:
            version = manifest.version
        state = self._live_state(name, version)
        if state not in (AgentLifecycle.CREATED, AgentLifecycle.VALIDATED,
                         AgentLifecycle.TESTED, AgentLifecycle.DISABLED):
            raise LifecycleError(
                f"agent {name!r} is {state.value}; validation only applies "
                f"to created/validated/tested/disabled agents")
        stored = self.store.load_manifest(name, version)
        stored.spec.validate()  # raises SpecError on any issue
        ok, detail = self.store.verify_integrity(name, version)
        if not ok:
            raise SpecError(f"package integrity failed for {name} "
                            f"v{version}: {detail}")
        if can_transition(state, AgentLifecycle.VALIDATED):
            return self._transition(name, AgentLifecycle.VALIDATED,
                                    event="validated", version=version,
                                    detail=detail)
        return self.show(name, version=version)

    def test(self, name: str, *, version: int | None = None,
             fabric: Any = None, approver: Callable | None = None
             ) -> dict[str, Any]:
        """Run the spec's benchmark suite and record the honest outcome.

        A passing run moves the version to ``tested``; a failing run
        moves (or keeps) it at ``validated`` and records the failures.
        Agents being benchmarked execute with operator approval
        (``approver``) supplied by the manager — this is a test rig, not
        an enabled agent.
        """
        manifest = self._require_agent(name)
        if version is None:
            version = manifest.version
        stored = self.store.load_manifest(name, version)
        state = self._live_state(name, version)
        if state not in (AgentLifecycle.CREATED, AgentLifecycle.VALIDATED,
                         AgentLifecycle.TESTED):
            raise LifecycleError(
                f"agent {name!r} is {state.value}; disable it before "
                f"re-testing")
        if state == AgentLifecycle.CREATED:
            # A raw package must be validated before anything runs.
            self.validate(name, version=version)
            stored = self.store.load_manifest(name, version)
        benchmark_id = stored.spec.verification.benchmark
        runtime = self.runtime_for(
            name, version=version, enabled=True, benchmark_rig=True,
            approver=approver if approver is not None else lambda *a: True,
            fabric=fabric)
        result = DEFAULT_HARNESS.run(benchmark_id, runtime, self.workspace)
        self.store.save_benchmarks(name, version, result.to_dict())
        meets, detail = DEFAULT_HARNESS.meets_requirements(
            result, stored.spec.verification)
        target = (AgentLifecycle.TESTED if meets
                  else AgentLifecycle.VALIDATED)
        payload = self._transition(name, target, event="tested" if meets
                                   else "test-failed", version=version,
                                   detail=detail)
        payload["benchmark"] = result.to_dict()
        payload["benchmark"]["meets_requirements"] = meets
        payload["benchmark"]["meets_detail"] = detail
        return payload

    def enable(self, name: str) -> dict[str, Any]:
        self._require_agent(name)
        state = self._live_state(name)
        if state != AgentLifecycle.TESTED:
            raise LifecycleError(
                f"agent {name!r} is {state.value}; only a fully tested "
                f"agent may be enabled")
        return self._transition(name, AgentLifecycle.ENABLED,
                                event="enabled")

    def pause(self, name: str) -> dict[str, Any]:
        return self._transition(name, AgentLifecycle.PAUSED,
                                event="paused")

    def resume(self, name: str) -> dict[str, Any]:
        return self._transition(name, AgentLifecycle.ENABLED,
                                event="resumed")

    def disable(self, name: str) -> dict[str, Any]:
        return self._transition(name, AgentLifecycle.DISABLED,
                                event="disabled")

    def retire(self, name: str) -> dict[str, Any]:
        return self._transition(name, AgentLifecycle.RETIRED,
                                event="retired")

    # -- inspection ------------------------------------------------------------

    def show(self, name: str, *, version: int | None = None
             ) -> dict[str, Any]:
        manifest = self.store.load_manifest(name, version)
        current_version, current_lifecycle = self.store.current(name)
        live = self._live_state(name, manifest.version)
        agent_dict = manifest.to_dict()
        agent_dict["lifecycle"] = live.value  # live state, not write-time
        ok, integrity = self.store.verify_integrity(name, manifest.version)
        return {
            "agent": agent_dict,
            "current_version": current_version,
            "current_lifecycle": current_lifecycle,
            "is_current": manifest.version == current_version,
            "runnable": (manifest.version == current_version
                         and is_runnable(current_lifecycle)),
            "integrity": {"ok": ok, "detail": integrity},
            "benchmarks": self.store.load_benchmarks(name, manifest.version),
            "history": self.store.history(name),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        for name in self.store.list_agents():
            try:
                manifest = self.store.load_manifest(name)
                current_version, current_lifecycle = self.store.current(name)
                agents.append({
                    "name": name,
                    "version": manifest.version,
                    "lifecycle": current_lifecycle,
                    "current_version": current_version,
                    "current_lifecycle": current_lifecycle,
                    "runnable": (manifest.version == current_version
                                 and is_runnable(current_lifecycle)),
                    "purpose": manifest.spec.purpose,
                    "template": manifest.template,
                    "tools": list(manifest.spec.tools),
                    "permissions": list(manifest.permissions),
                    "capabilities": list(manifest.spec.capabilities),
                    "created_by": manifest.created_by,
                })
            except AgentNotFoundError:
                continue
        return agents

    def versions(self, name: str) -> list[dict[str, Any]]:
        self._require_agent(name)
        current_version, current_lifecycle = self.store.current(name)
        payload: list[dict[str, Any]] = []
        for version in self.store.versions(name):
            manifest = self.store.load_manifest(name, version)
            payload.append({
                "version": version,
                "lifecycle": self._live_state(name, version).value,
                "is_current": version == current_version,
                "created_at": manifest.created_at,
                "created_by": manifest.created_by,
                "changelog": manifest.changelog,
                "operator_confirmed": manifest.operator_confirmed,
                "permission_digest": manifest.permission_digest,
                "parent_version": manifest.parent_version,
                "benchmarks": self.store.load_benchmarks(name, version),
            })
        return payload

    def templates(self) -> list[dict[str, Any]]:
        return template_catalog()

    def export_package(self, name: str, *, version: int | None = None
                       ) -> dict[str, Any]:
        return self.factory.export_package(name, version)

    # -- execution --------------------------------------------------------------

    def runtime_for(self, name: str, *, version: int | None = None,
                    enabled: bool | None = None, approver: Callable | None
                    = None, fabric: Any = None,
                    benchmark_rig: bool = False, **components: Any
                    ) -> AgentRuntime:
        """Build the runtime an agent's task loop executes through.

        ``enabled`` defaults to the truth: the store's current lifecycle
        must be ENABLED and the version must be current. ``benchmark_rig``
        is an internal escape hatch used only by :meth:`test` to execute
        an agent that has not been enabled yet — it is never exposed to
        agents.
        """
        manifest = self.store.load_manifest(name, version)
        if version is None:
            version = manifest.version
        current_version, current_lifecycle = self.store.current(name)
        runnable_now = (version == current_version
                        and is_runnable(current_lifecycle))
        if enabled is None:
            enabled = runnable_now
        elif enabled and not runnable_now and not benchmark_rig:
            raise NotRunnableError(
                f"agent {name!r} v{version} is not enabled in the store; "
                f"only the manager may enable it")
        if fabric is None:
            fabric = self.fabric
        return AgentRuntime(
            manifest, workspace=self.workspace, fabric=fabric,
            enabled=bool(enabled), approver=approver,
            lifecycle=self._live_state(name, version).value, **components)
