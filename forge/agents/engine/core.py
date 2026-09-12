"""The Agent Creation Engine façade (A81).

:class:`AgentCreationEngine` wires the pieces together — package store,
factory, grant ledger, governor, memory, tool runtime, PolicyGate, and the
Model Fabric — and is what the CLI, the desktop Agent Manager, and the
control plane talk to. It owns no trust of its own: every action it takes
goes through the factory's validation, the lifecycle gates, the ledger's
no-self-grant rule, and the PolicyGate.

Typical operator flow::

    engine = AgentCreationEngine("/path/to/project")
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    engine.grant_spec("exporter", actor="alice")
    engine.test("exporter", actor="alice")
    engine.enable("exporter", actor="alice")
    engine.run("exporter", "add CSV export", actor="alice")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from forge.agents.engine.errors import AgentNotFoundError
from forge.agents.engine.factory import AgentFactory
from forge.agents.engine.governor import AgentGovernor
from forge.agents.engine.grants import GrantLedger
from forge.agents.engine.lifecycle import AgentState
from forge.agents.engine.memory import AgentMemory
from forge.agents.engine.package import AgentPackage, PackageStore
from forge.agents.engine.runtime import AgentRuntime
from forge.agents.engine.spec import AgentSpec
from forge.agents.engine.templates import describe_templates, template_ids


class AgentCreationEngine:
    """One engine per project root."""

    def __init__(self, root: str = ".", *, fabric: Any = None,
                 permission_manager: Any = None, tool_runtime: Any = None,
                 memory_store: Any = None, audit: Any = None) -> None:
        self.root = str(Path(root).resolve())
        self.governor = AgentGovernor()
        self.store = PackageStore(self.root)
        self.runtime = AgentRuntime(
            root=self.root, store=self.store, fabric=fabric,
            permission_manager=permission_manager,
            tool_runtime=tool_runtime, memory_store=memory_store,
            governor=self.governor, audit=audit)
        self.factory = AgentFactory(self.root, store=self.store,
                                    runtime=self.runtime)

    # -- construction ----------------------------------------------------

    def create(self, spec: AgentSpec, *, actor: str,
               grant: bool = False) -> dict:
        package = self.factory.create(spec, actor=actor, grant=grant)
        return package.summary()

    def create_from_template(self, template_id: str, name: str, *,
                             purpose: str = "", actor: str = "operator",
                             overrides: Any = None,
                             grant: bool = False) -> dict:
        package = self.factory.create_from_template(
            template_id, name, purpose=purpose, actor=actor,
            overrides=overrides, grant=grant)
        return package.summary()

    def create_from_dict(self, payload: Any, *, actor: str,
                         grant: bool = False) -> dict:
        package = self.factory.create_from_dict(payload, actor=actor,
                                                grant=grant)
        return package.summary()

    # -- reads -----------------------------------------------------------

    def names(self) -> list:
        return self.store.names()

    def list(self, state: str = "") -> list:
        return self.factory.list(state=state)

    def get(self, name: str) -> AgentPackage:
        return self.factory.get(name)

    def detail(self, name: str) -> dict:
        """Everything the Agent Manager shows about one agent."""
        package = self.factory.get(name)
        ledger = GrantLedger(package.name, package.spec,
                             self.store.read_grants(name))
        memory = AgentMemory(self.runtime.memory_store(), package.name,
                             package.spec.memory)
        benchmarks = self.store.benchmarks(name, limit=1)
        return {
            "summary": package.summary(),
            "spec": package.spec.to_dict(),
            "lifecycle": package.lifecycle.to_dict(),
            "allowed_transitions": list(package.lifecycle.allowed()),
            "permissions": self.factory.effective_permissions(name),
            "grants": ledger.to_dict(),
            "versions": self.store.list_versions(name),
            "benchmark": benchmarks[0] if benchmarks else {},
            "history": self.store.history(name, limit=10),
            "memory": memory.usage(),
            "usage": self.governor.usage(package.name, package.spec.limits),
            "directory": str(package.directory.relative_to(
                Path(self.root))) if package.directory.is_absolute()
            else str(package.directory),
        }

    def summary(self) -> dict:
        """Overview for the desktop Agent Manager and ``forge agents``."""
        agents = self.list()
        by_state: dict = {}
        for agent in agents:
            by_state[agent["state"]] = by_state.get(agent["state"], 0) + 1
        return {
            "root": self.root,
            "agents": agents,
            "templates": describe_templates(),
            "counts": {"total": len(agents), "by_state": by_state},
            "states": list(AgentState.ALL),
            "runnable_states": list(AgentState.RUNNABLE),
        }

    def templates(self) -> list:
        return describe_templates()

    def template_ids(self) -> tuple:
        return template_ids()

    # -- lifecycle -------------------------------------------------------

    def validate(self, name: str, *, actor: str) -> dict:
        return self.factory.validate(name, actor=actor)

    def test(self, name: str, *, actor: str,
             include_model_checks: bool = True) -> dict:
        return self.factory.test(name, actor=actor,
                                 include_model_checks=include_model_checks)

    def enable(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self.factory.enable(name, actor=actor, reason=reason)

    def pause(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self.factory.pause(name, actor=actor, reason=reason)

    def resume(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self.factory.resume(name, actor=actor, reason=reason)

    def disable(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self.factory.disable(name, actor=actor, reason=reason)

    def retire(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self.factory.retire(name, actor=actor, reason=reason)

    def set_state(self, name: str, state: str, *, actor: str,
                  reason: str = "") -> dict:
        """Move to an explicit state (used by the desktop buttons)."""
        handlers = {AgentState.ENABLED: self.enable,
                    AgentState.PAUSED: self.pause,
                    AgentState.DISABLED: self.disable,
                    AgentState.RETIRED: self.retire}
        handler = handlers.get(state)
        if handler is None:
            raise ValueError(
                "State %r cannot be set directly; use validate/test" % state)
        if state == AgentState.ENABLED and \
                self.get(name).state == AgentState.PAUSED:
            return self.resume(name, actor=actor, reason=reason)
        return handler(name, actor=actor, reason=reason)

    # -- specification & versioning --------------------------------------

    def update_spec(self, name: str, spec: AgentSpec, *, actor: str,
                    notes: str = "") -> dict:
        return self.factory.update_spec(name, spec, actor=actor, notes=notes)

    def versions(self, name: str) -> list:
        return self.factory.versions(name)

    def version(self, name: str, version: str) -> dict:
        return self.factory.version(name, version)

    def revert_to_version(self, name: str, version: str, *,
                          actor: str) -> dict:
        return self.factory.revert_to_version(name, version, actor=actor)

    # -- permissions -----------------------------------------------------

    def permissions(self, name: str) -> dict:
        return self.factory.effective_permissions(name)

    def grant(self, name: str, operation: str, *, actor: str,
              reason: str = "", ttl_seconds: float = 0.0) -> dict:
        return self.factory.grant(name, operation, actor=actor,
                                  reason=reason, ttl_seconds=ttl_seconds)

    def grant_spec(self, name: str, *, actor: str,
                   reason: str = "") -> list:
        return self.factory.grant_spec(name, actor=actor, reason=reason)

    def revoke(self, name: str, operation: str, *, actor: str,
               reason: str = "") -> dict:
        return self.factory.revoke(name, operation, actor=actor,
                                   reason=reason)

    # -- execution -------------------------------------------------------

    def run(self, name: str, task: str, *, actor: str = "operator",
            approved: bool = False, task_id: str = "",
            instructions: str = "") -> dict:
        result = self.runtime.run(name, task, actor=actor, approved=approved,
                                  task_id=task_id, instructions=instructions)
        return result.to_dict()

    def history(self, name: str, limit: int = 20) -> list:
        return self.factory.history(name, limit=limit)

    def benchmarks(self, name: str, limit: int = 5) -> list:
        return self.factory.benchmarks(name, limit=limit)

    def delete(self, name: str, *, actor: str) -> dict:
        return self.factory.delete(name, actor=actor)

    def exists(self, name: str) -> bool:
        return self.store.exists(name)

    def require(self, name: str) -> AgentPackage:
        if not self.store.exists(name):
            raise AgentNotFoundError("No such agent: %s" % name)
        return self.store.load(name)
