"""The Forge Agent Creation Engine (A81).

:class:`AgentCreationEngine` is the first-party entry point that ties
the pieces together:

``create``    spec (or template) -> validated structured package
``validate``  structural + security validation, ``created -> validated``
``test``      the code-judged benchmark, ``validated -> tested``
``enable``    operator-only, ``tested|paused -> enabled``
``pause`` / ``disable`` / ``retire``
``revise``    a new version whose bump level is derived from the diff
``bind``      a :class:`BoundAgent` wired to the live subsystems

Security posture:

* An agent is never runnable until an operator explicitly enables a
  package that passed validation *and* the benchmark.
* :meth:`grant_permission` does not exist. Permissions live in the
  specification, and changing them forces a MAJOR version, which resets
  the lifecycle to ``created`` and requires re-validation and re-test.
* Every state change is recorded with its actor.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from forge.agent_engine.benchmark import AgentBenchmark
from forge.agent_engine.factory import AgentFactoryEngine
from forge.agent_engine.lifecycle import (
    CREATED,
    DISABLED,
    ENABLED,
    PAUSED,
    RETIRED,
    TESTED,
    VALIDATED,
    LifecycleError,
    LifecycleManager,
)
from forge.agent_engine.package import AgentPackage
from forge.agent_engine.runtime import BoundAgent
from forge.agent_engine.spec import AgentSpec, SpecError
from forge.agent_engine.templates import TEMPLATE_NAMES, from_template
from forge.agent_engine.validator import PackageValidator
from forge.agent_engine.version import (
    MAJOR,
    AgentVersion,
    VersionHistory,
    classify_change,
)

MAX_AGENTS = 64
STORE_FILENAME = "agents.json"


class EngineError(RuntimeError):
    """An engine operation was refused, with an honest reason."""


class AgentCreationEngine:
    """Create, validate, test, version, and gate specialized agents."""

    def __init__(self, *, factory=None, validator=None, benchmark=None,
                 lifecycle=None, fabric=None, policy_gate=None,
                 tool_runtime=None, memory_store=None, verification=None,
                 checkpoints=None, store_dir: str = "") -> None:
        self.factory = factory or AgentFactoryEngine()
        self.validator = validator or PackageValidator()
        self.benchmark = benchmark or AgentBenchmark(self.validator)
        self.lifecycle = lifecycle or LifecycleManager()
        self.fabric = fabric
        self.policy_gate = policy_gate
        self.tool_runtime = tool_runtime
        self.memory_store = memory_store
        self.verification = verification
        self.checkpoints = checkpoints
        self.store_dir = str(store_dir or "")
        self._packages: dict = {}
        self._versions: dict = {}
        self._archive: dict = {}
        self._events: list = []

    # -- helpers ---------------------------------------------------------

    def _record(self, kind: str, agent: str, actor: str,
                detail: Any = None) -> None:
        self._events.append({"event": kind, "agent": agent,
                             "actor": actor, "detail": detail,
                             "at": time.time()})

    def _require(self, name: str) -> AgentPackage:
        package = self._packages.get(name)
        if package is None:
            raise EngineError("Unknown agent: {0!r}".format(name))
        return package

    def _sync_state(self, package: AgentPackage) -> None:
        package.state = self.lifecycle.state(package.name)

    # -- creation --------------------------------------------------------

    def templates(self) -> list:
        return list(TEMPLATE_NAMES)

    def create(self, spec: Any = None, *, template: str = "",
               overrides: Mapping = None, actor: str = "operator",
               version: Any = "1.0.0") -> AgentPackage:
        if template:
            spec = from_template(template, overrides)
        elif isinstance(spec, Mapping):
            spec = AgentSpec.from_dict(spec)
        if not isinstance(spec, AgentSpec):
            raise SpecError(
                "create() needs an AgentSpec, a mapping, or a template")
        if spec.name in self._packages:
            raise EngineError(
                "Agent {0!r} already exists; use revise() for a new "
                "version.".format(spec.name))
        if len(self._packages) >= MAX_AGENTS:
            raise EngineError(
                "Agent limit reached ({0})".format(MAX_AGENTS))

        package = self.factory.build(spec, version=version,
                                     built_by=actor)
        self._packages[spec.name] = package
        history = VersionHistory(spec.name)
        history.record(package.version, spec, level="initial",
                       note="created")
        self._versions[spec.name] = history
        self._archive[spec.name] = {str(package.version): package}
        self.lifecycle.register(spec.name, CREATED)
        self._sync_state(package)
        self._record("created", spec.name, actor,
                     {"version": str(package.version),
                      "template": spec.template})
        return package

    # -- lifecycle -------------------------------------------------------

    def validate(self, name: str, *, actor: str = "operator") -> dict:
        package = self._require(name)
        result = self.validator.validate(package)
        package.validation = result.to_dict()
        if not result.valid:
            self._record("validation_failed", name, actor,
                         package.validation)
            return {"agent": name, "valid": False,
                    "state": self.lifecycle.state(name),
                    **package.validation}
        if self.lifecycle.state(name) in (CREATED, DISABLED, TESTED):
            self.lifecycle.transition(name, VALIDATED, actor=actor,
                                      reason="validation passed")
        self._sync_state(package)
        self._record("validated", name, actor, package.validation)
        return {"agent": name, "valid": True,
                "state": self.lifecycle.state(name), **package.validation}

    def test(self, name: str, *, actor: str = "operator",
             fabric: Any = None, include_behavioural: bool = True) -> dict:
        package = self._require(name)
        state = self.lifecycle.state(name)
        if state == CREATED:
            outcome = self.validate(name, actor=actor)
            if not outcome["valid"]:
                return {"agent": name, "passed": False,
                        "state": self.lifecycle.state(name),
                        "reason": "validation failed",
                        "validation": outcome}
            state = self.lifecycle.state(name)
        if state not in (VALIDATED, TESTED, ENABLED, PAUSED):
            raise EngineError(
                "Agent {0!r} is {1}; it must be validated before "
                "testing.".format(name, state))

        report = self.benchmark.run(
            package, fabric=fabric if fabric is not None else self.fabric,
            agent=self.bind(name, enforce_state=False),
            include_behavioural=include_behavioural)
        package.benchmark = report.to_dict()
        if report.passed and self.lifecycle.state(name) == VALIDATED:
            self.lifecycle.transition(name, TESTED, actor=actor,
                                      reason="benchmark passed")
        self._sync_state(package)
        self._record("tested", name, actor,
                     {"passed": report.passed, "score": report.score})
        return {"agent": name, "state": self.lifecycle.state(name),
                **package.benchmark}

    def enable(self, name: str, *, actor: str = "operator") -> dict:
        package = self._require(name)
        state = self.lifecycle.state(name)
        if state == PAUSED:
            self.lifecycle.transition(name, ENABLED, actor=actor,
                                      reason="resumed")
        else:
            if state != TESTED:
                raise EngineError(
                    "Agent {0!r} is {1}; only a tested agent may be "
                    "enabled.".format(name, state))
            if not (package.benchmark or {}).get("passed"):
                raise EngineError(
                    "Agent {0!r} has no passing benchmark; run "
                    "`forge agents test {0}` first.".format(name))
            if not (package.validation or {}).get("valid"):
                raise EngineError(
                    "Agent {0!r} has no passing validation.".format(name))
            self.lifecycle.transition(name, ENABLED, actor=actor,
                                      reason="operator enabled")
        self._sync_state(package)
        self._record("enabled", name, actor, None)
        return {"agent": name, "state": self.lifecycle.state(name)}

    def pause(self, name: str, *, actor: str = "operator") -> dict:
        package = self._require(name)
        self.lifecycle.transition(name, PAUSED, actor=actor,
                                  reason="operator paused")
        self._sync_state(package)
        self._record("paused", name, actor, None)
        return {"agent": name, "state": self.lifecycle.state(name)}

    def disable(self, name: str, *, actor: str = "operator") -> dict:
        package = self._require(name)
        self.lifecycle.transition(name, DISABLED, actor=actor,
                                  reason="operator disabled")
        self._sync_state(package)
        self._record("disabled", name, actor, None)
        return {"agent": name, "state": self.lifecycle.state(name)}

    def retire(self, name: str, *, actor: str = "operator") -> dict:
        package = self._require(name)
        self.lifecycle.transition(name, RETIRED, actor=actor,
                                  reason="operator retired")
        self._sync_state(package)
        self._record("retired", name, actor, None)
        return {"agent": name, "state": self.lifecycle.state(name)}

    # -- versioning ------------------------------------------------------

    def revise(self, name: str, changes: Mapping, *,
               actor: str = "operator", note: str = "") -> AgentPackage:
        package = self._require(name)
        if self.lifecycle.state(name) == RETIRED:
            raise EngineError(
                "Agent {0!r} is retired and cannot be revised.".format(
                    name))
        new_spec = package.spec.with_changes(**dict(changes or {}))
        level = classify_change(package.spec, new_spec)
        new_version = AgentVersion.parse(package.version).bump(level)
        new_package = self.factory.build(new_spec, version=new_version,
                                         built_by=actor)
        self._packages[name] = new_package
        self._archive.setdefault(name, {})[str(new_version)] = new_package
        self._versions[name].record(new_version, new_spec, level=level,
                                    note=note or "revised")

        # Any change re-opens the lifecycle; a widening (MAJOR) change
        # always drops back to `created` so nothing inherits trust.
        current = self.lifecycle.state(name)
        if level == MAJOR or current in (ENABLED, PAUSED, TESTED):
            self.lifecycle.register(name, CREATED)
        new_package.state = self.lifecycle.state(name)
        self._record("revised", name, actor,
                     {"level": level, "version": str(new_version)})
        return new_package

    def versions(self, name: str) -> list:
        self._require(name)
        return self._versions[name].entries()

    def package_at(self, name: str, version: Any) -> AgentPackage:
        self._require(name)
        key = str(AgentVersion.parse(version))
        package = self._archive.get(name, {}).get(key)
        if package is None:
            raise EngineError(
                "Agent {0!r} has no version {1}".format(name, key))
        return package

    def rollback_to(self, name: str, version: Any, *,
                    actor: str = "operator") -> AgentPackage:
        """Restore an older package. It must be re-validated and retested."""
        old = self.package_at(name, version)
        restored = self.factory.build(old.spec, version=old.version,
                                      built_by=actor)
        self._packages[name] = restored
        self.lifecycle.register(name, CREATED)
        restored.state = CREATED
        self._versions[name].record(old.version, old.spec,
                                    level="rollback",
                                    note="rolled back to {0}".format(
                                        old.version))
        self._record("rolled_back", name, actor,
                     {"version": str(old.version)})
        return restored

    # -- access ----------------------------------------------------------

    def get(self, name: str) -> AgentPackage:
        return self._require(name)

    def list(self) -> list:
        out = []
        for name in sorted(self._packages):
            package = self._packages[name]
            self._sync_state(package)
            out.append({
                "name": name,
                "version": str(package.version),
                "state": package.state,
                "template": package.spec.template,
                "purpose": package.spec.purpose,
                "capabilities": list(package.spec.capabilities),
                "tools": list(package.spec.tools),
                "validated": bool((package.validation or {}).get("valid")),
                "benchmark_passed": bool(
                    (package.benchmark or {}).get("passed")),
                "package_id": package.package_id(),
            })
        return out

    def list_names(self) -> list:
        return sorted(self._packages)

    def status(self, name: str) -> dict:
        package = self._require(name)
        self._sync_state(package)
        return {
            **package.to_dict(),
            "versions": self.versions(name),
            "history": self.lifecycle.history(name),
            "runnable": self.lifecycle.can_run(name),
        }

    def events(self) -> list:
        return [dict(event) for event in self._events]

    def bind(self, name: str, *, enforce_state: bool = True) -> BoundAgent:
        package = self._require(name)
        if enforce_state and not self.lifecycle.can_run(name):
            raise EngineError(
                "Agent {0!r} is {1}; only enabled agents may be "
                "bound for work.".format(name, self.lifecycle.state(name)))
        return BoundAgent(
            package, fabric=self.fabric, policy_gate=self.policy_gate,
            tool_runtime=self.tool_runtime,
            memory_store=self.memory_store,
            verification=self.verification, checkpoints=self.checkpoints,
            lifecycle=self.lifecycle if enforce_state else None)

    # -- persistence ------------------------------------------------------

    def export(self, name: str) -> dict:
        return self._require(name).to_dict()

    def import_package(self, payload: Any, *,
                       actor: str = "operator") -> AgentPackage:
        """Import an exported package. It always arrives untrusted."""
        package = AgentPackage.from_dict(payload)
        if package.name in self._packages:
            raise EngineError(
                "Agent {0!r} already exists".format(package.name))
        self._packages[package.name] = package
        history = VersionHistory(package.name)
        history.record(package.version, package.spec, level="imported",
                       note="imported")
        self._versions[package.name] = history
        self._archive[package.name] = {str(package.version): package}
        self.lifecycle.register(package.name, CREATED)
        self._record("imported", package.name, actor, None)
        return package

    def save(self, path: str = "") -> str:
        target = Path(path or (Path(self.store_dir or ".forge/agents")
                               / STORE_FILENAME))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": "forge-agent-store", "format_version": 1,
                   "agents": [self.export(name)
                              for name in sorted(self._packages)],
                   "states": self.lifecycle.snapshot()}
        target.write_text(json.dumps(payload, indent=2, sort_keys=True),
                          encoding="utf-8")
        return str(target)

    def load(self, path: str = "") -> list:
        source = Path(path or (Path(self.store_dir or ".forge/agents")
                               / STORE_FILENAME))
        if not source.exists():
            return []
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("format") != "forge-agent-store":
            raise EngineError("Not a forge-agent-store file")
        loaded = []
        for item in payload.get("agents", ()):
            try:
                package = self.import_package(item)
            except (EngineError, SpecError, LifecycleError):
                continue
            loaded.append(package.name)
        return loaded
