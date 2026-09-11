"""Agent Manager backend for the desktop app (A81).

Headless, GUI-free logic behind the desktop "Agents" panel: list agents,
create from a template, validate, benchmark, and drive the lifecycle.
Every method returns plain dict/list data and raises
:class:`AgentManagerError` with an actionable message, mirroring the
rest of :mod:`forge.desktop_app.backend`.

The manager owns an :class:`AgentCreationEngine` and persists it to the
same JSON store the ``forge agents`` CLI uses, so the two surfaces
always agree. It exposes no way to grant permissions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from forge.agent_engine.cli import DEFAULT_STORE, load_engine
from forge.agent_engine.engine import EngineError
from forge.agent_engine.lifecycle import LifecycleError
from forge.agent_engine.spec import SpecError
from forge.agent_engine.templates import TEMPLATE_NAMES, TEMPLATES
from forge.agent_engine.version import VersionError

_ERRORS = (EngineError, SpecError, LifecycleError, VersionError,
           OSError, ValueError)


class AgentManagerError(RuntimeError):
    """A user-facing agent-management failure."""


class AgentManager:
    """Desktop-facing facade over the Agent Creation Engine."""

    def __init__(self, store: str = "", *, fabric: Any = None) -> None:
        self.store = str(store or DEFAULT_STORE)
        self.fabric = fabric
        self._engine = None

    # -- engine ----------------------------------------------------------

    @property
    def engine(self):
        if self._engine is None:
            try:
                self._engine = load_engine(self.store)
            except _ERRORS as exc:
                raise AgentManagerError(
                    "Cannot open the agent store: {0}".format(exc))
            self._engine.fabric = self.fabric
        return self._engine

    def _save(self) -> None:
        try:
            Path(self.store).parent.mkdir(parents=True, exist_ok=True)
            self.engine.save(self.store)
        except _ERRORS as exc:
            raise AgentManagerError(
                "Cannot write the agent store: {0}".format(exc))

    def reload(self) -> list:
        self._engine = None
        return self.list_agents()

    # -- read ------------------------------------------------------------

    def templates(self) -> list:
        return [{"template": name,
                 "name": TEMPLATES[name]["name"],
                 "purpose": TEMPLATES[name]["purpose"],
                 "capabilities": list(TEMPLATES[name]["capabilities"]),
                 "tools": list(TEMPLATES[name]["tools"])}
                for name in TEMPLATE_NAMES]

    def list_agents(self) -> list:
        try:
            return self.engine.list()
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))

    def get_agent(self, name: str) -> dict:
        try:
            return self.engine.status(name)
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))

    def summary(self, name: str) -> str:
        """Human-readable detail text for the Agent Manager panel."""
        data = self.get_agent(name)
        spec = data["spec"]
        manifest = data["manifest"]
        benchmark = data.get("benchmark") or {}
        validation = data.get("validation") or {}
        lines = [
            "{0}  v{1}  [{2}]".format(manifest["name"],
                                      manifest["version"],
                                      manifest["state"]),
            "package {0}".format(manifest["package_id"]),
            "",
            "Purpose:      {0}".format(spec["purpose"]),
            "Capabilities: {0}".format(", ".join(spec["capabilities"])),
            "Tools:        {0}".format(
                ", ".join(spec["tools"]) or "none"),
            "",
            "Permissions",
            "  read:     {0}".format(
                ", ".join(spec["permissions"]["read_paths"]) or "none"),
            "  write:    {0}".format(
                ", ".join(spec["permissions"]["write_paths"]) or "none"),
            "  network:  {0}".format(
                spec["permissions"]["allow_network"]),
            "  terminal: {0}".format(
                spec["permissions"]["allow_terminal"]),
            "  approval required for writes: {0}".format(
                spec["permissions"]["require_approval_for_writes"]),
            "",
            "Memory:       {0} (max {1} entries)".format(
                spec["memory_policy"]["scope"],
                spec["memory_policy"]["max_entries"]),
            "Gates:        {0}".format(
                ", ".join(spec["verification"]["required_gates"])),
            "Limits:       {0} runs/h, {1} concurrent, {2}s".format(
                spec["resource_limits"]["max_runs_per_hour"],
                spec["resource_limits"]["max_concurrent_runs"],
                spec["resource_limits"]["max_wall_seconds"]),
            "",
            "Validation:   {0}".format(
                "valid" if validation.get("valid") else
                ("not run" if not validation else "INVALID")),
            "Benchmark:    {0}".format(
                "{0} ({1}/{2})".format(
                    "passed" if benchmark.get("passed") else "failed",
                    benchmark.get("passed_count", 0),
                    benchmark.get("total", 0))
                if benchmark else "not run"),
            "Runnable:     {0}".format(data.get("runnable")),
        ]
        for finding in validation.get("findings", ()):
            lines.append("  [{0}] {1}".format(finding["severity"],
                                              finding["message"]))
        for check in benchmark.get("failures", ()):
            lines.append("  FAIL {0}: {1}".format(check["name"],
                                                  check["details"]))
        lines.append("")
        lines.append("Versions")
        for entry in data.get("versions", ()):
            lines.append("  {0:<8} {1:<9} {2}".format(
                entry["version"], entry["level"], entry["note"]))
        return "\n".join(lines)

    # -- write -------------------------------------------------------------

    def create(self, template: str, *, name: str = "",
               purpose: str = "", actor: str = "desktop") -> dict:
        overrides = {}
        if name:
            overrides["name"] = name
        if purpose:
            overrides["purpose"] = purpose
        try:
            package = self.engine.create(template=template,
                                         overrides=overrides,
                                         actor=actor)
            validation = self.engine.validate(package.name, actor=actor)
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))
        self._save()
        return {"agent": package.name, "version": str(package.version),
                "state": package.state, "validation": validation}

    def validate(self, name: str, *, actor: str = "desktop") -> dict:
        try:
            result = self.engine.validate(name, actor=actor)
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))
        self._save()
        return result

    def test(self, name: str, *, actor: str = "desktop",
             include_behavioural: bool = False) -> dict:
        try:
            report = self.engine.test(
                name, actor=actor,
                include_behavioural=include_behavioural)
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))
        self._save()
        return report

    def _transition(self, action: str, name: str, actor: str) -> dict:
        try:
            result = getattr(self.engine, action)(name, actor=actor)
        except _ERRORS as exc:
            raise AgentManagerError(str(exc))
        self._save()
        return result

    def enable(self, name: str, *, actor: str = "desktop") -> dict:
        return self._transition("enable", name, actor)

    def disable(self, name: str, *, actor: str = "desktop") -> dict:
        return self._transition("disable", name, actor)

    def pause(self, name: str, *, actor: str = "desktop") -> dict:
        return self._transition("pause", name, actor)

    def retire(self, name: str, *, actor: str = "desktop") -> dict:
        return self._transition("retire", name, actor)
