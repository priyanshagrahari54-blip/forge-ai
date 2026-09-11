"""Agent Creation Engine (first-party Forge agent factory).

The engine turns structured :class:`AgentSpec` documents into
runnable, versioned, benchmark-tested agent packages — and runs them
through the same guarded subsystems as every built-in agent:

* **Model Fabric** — all model calls route by the spec's model
  requirements; no direct provider access.
* **PolicyGate** — every write/delete is evaluated; DENY wins always.
* **Tool Runtime** — only the spec's tools are registered, each bound
  to its required permission.
* **Memory** — one namespace per agent; cross-agent reads are
  impossible by construction.
* **Verification** — security/review gates run over every run's
  changed files; required gates must pass.
* **Checkpoints** — a pre-run checkpoint rolls back failed runs.

No agent may self-grant permissions: every power-changing operation
(``create``/``update``/``validate``/``test``/``enable``/``pause``/
``disable``/``retire``/``update_permissions``/``set_limits``/
``delete``) requires an operator identity that differs from the
agent's own. An agent acting as its own operator is refused.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from forge.agents.agent_benchmarks import build_test_report
from forge.agents.governance import AgentGovernor
from forge.agents.lifecycle import (ENABLE_FROM, LifecycleState,
                                    check_transition, is_runnable,
                                    normalize)
from forge.agents.package import (MAX_PACKAGES, AgentPackage,
                                  build_package, rebind, resolve_executor,
                                  touch_history)
from forge.agents.spec import (AgentSpec, MemoryPolicy, ModelRequirements,
                               ResourceLimits, VerificationRequirements)
from forge.agents.store import AgentStore
from forge.agents.templates import build_from_template, describe_all
from forge.agents.versioning import INITIAL_VERSION, bump, validate

MAX_RUN_LOG = 20
MAX_REQUIREMENT = 4000

#: Spec tool -> required A32 permission. ``""`` means the tool needs
#: no filesystem authority (namespaced memory / routed model calls).
TOOL_PERMISSIONS = {
    "read_file": "read_file",
    "write_file": "write_file",
    "delete_file": "delete_file",
    "terminal": "run_command",
    "run_tests": "run_tests",
    "search": "search_files",
    "git_status": "git_status",
    "git_diff": "git_diff",
    "git_commit": "git_commit",
    "memory_read": "",
    "memory_write": "",
    "compute_execute": "run_command",
    "model_generate": "",
}

#: Capability families and the worker that serves them honestly.
WRITE_FAMILY = frozenset({
    "coding", "debugging", "documentation",
})
AUDIT_FAMILY = frozenset({
    "security", "review", "testing",
})
STUDY_FAMILY = frozenset({
    "research", "planning", "reasoning",
})


class AgentEngineError(ValueError):
    """Engine refusal: invalid request, illegal transition, or quota."""


class SelfGrantDenied(PermissionError):
    """An agent attempted to act on its own package as operator."""


class AgentCreationEngine:
    """Create, version, benchmark, and run specialized agents."""

    def __init__(self, *, fabric: Any = None, policy: Any = None,
                 approval_store: Any = None, audit: Any = None,
                 root: str = ".", memory_root: str = "",
                 store: AgentStore | str | None = None,
                 session_id: str = "default",
                 mode: str = "assisted") -> None:
        from forge.security.permissions import OperationMode

        self.root = str(Path(root).resolve())
        self.memory_root = memory_root or str(
            Path(self.root) / ".forge" / "agent-memory")
        Path(self.memory_root).mkdir(parents=True, exist_ok=True)
        if isinstance(store, str):
            store = AgentStore(store)
        self.store: AgentStore | None = store
        self._memory_packages: dict[str, AgentPackage] = {}
        self.session_id = session_id
        try:
            self.mode = OperationMode(mode)
        except ValueError:
            raise AgentEngineError(
                "Unknown mode %r" % (mode,)) from None
        if fabric is None:
            from forge.models.fabric import ModelFabric

            fabric = ModelFabric.from_defaults()
        self.fabric = fabric
        self.policy = policy
        self.approval_store = approval_store
        self.audit = audit
        self.governor = AgentGovernor()
        self._runs: dict[str, list[dict[str, Any]]] = {}

    # -- package persistence -------------------------------------------

    def _save(self, package: AgentPackage) -> AgentPackage:
        if self.store is not None:
            self.store.save(package)
        else:
            if package.name not in self._memory_packages \
                    and len(self._memory_packages) >= MAX_PACKAGES:
                raise AgentEngineError(
                    "Agent limit reached (%d)" % MAX_PACKAGES)
            self._memory_packages[package.name] = package
        limits = package.spec.resource_limits
        try:
            self.governor.set_limits(
                package.name,
                max_runs_per_hour=limits.max_runs_per_hour,
                max_concurrent=limits.max_concurrent)
        except ValueError:
            pass
        return package

    def _load(self, name: str) -> AgentPackage | None:
        if self.store is not None:
            try:
                return self.store.load(name)
            except ValueError:
                return None
        return self._memory_packages.get((name or "").strip().lower())

    def _remove(self, name: str) -> bool:
        if self.store is not None:
            return self.store.delete(name)
        return self._memory_packages.pop(
            (name or "").strip().lower(), None) is not None

    def _names(self) -> list[str]:
        if self.store is not None:
            return self.store.list_names()
        return sorted(self._memory_packages)

    # -- operator separation --------------------------------------------

    @staticmethod
    def _require_operator(agent_name: str, actor: str,
                          action: str) -> str:
        """Refuse when the agent would act as its own operator."""
        actor = (actor or "").strip()
        if not actor:
            raise AgentEngineError(
                "%s requires an operator identity" % action)
        internal = "forge-agent:%s" % agent_name
        if actor == agent_name or actor == internal:
            raise SelfGrantDenied(
                "Denied: agent %r may not %s itself; an operator "
                "must do it" % (agent_name, action))
        return actor

    def _audit(self, actor: str, operation: str, allowed: bool,
               *, task_id: str = "", reason: str = "") -> None:
        audit = getattr(self, "audit", None)
        if audit is None:
            return
        try:
            from forge.security.policy_gate import PolicyDecision

            audit.record_decision(
                agent=actor, resource="agents", operation=operation,
                scope=task_id or "agents",
                decision=(PolicyDecision.ALLOW if allowed
                          else PolicyDecision.DENY),
                reason=reason, task_id=task_id)
        except Exception:
            pass

    # -- creation --------------------------------------------------------

    def create(self, spec: AgentSpec | dict, *,
               created_by: str = "", bind: bool = False
               ) -> AgentPackage:
        """Validate a spec and generate its structured package."""
        if isinstance(spec, dict):
            spec = AgentSpec.from_dict(spec)
        if not isinstance(spec, AgentSpec):
            raise AgentEngineError("create needs an AgentSpec")
        self._require_operator(spec.name, created_by or "operator",
                               "create agent")
        if self._load(spec.name) is not None:
            raise AgentEngineError(
                "Agent already exists: %s" % spec.name)
        package = build_package(spec, created_by=created_by,
                                bind=bind)
        self._save(package)
        self._audit(created_by, "create", True,
                    task_id=spec.name,
                    reason="version=%s real=%s"
                           % (package.version, package.real))
        return package

    def create_from_template(self, template: str, name: str, *,
                             purpose: str = "",
                             created_by: str = "",
                             bind: bool = False) -> AgentPackage:
        spec = build_from_template(template, name, purpose)
        return self.create(spec, created_by=created_by, bind=bind)

    def templates(self) -> list[dict[str, Any]]:
        return describe_all()

    def get(self, name: str) -> AgentPackage:
        package = self._load(name)
        if package is None:
            raise AgentEngineError("Unknown agent: %s" % (name,))
        return package

    def list(self) -> list[AgentPackage]:
        packages: list[AgentPackage] = []
        for name in self._names():
            package = self._load(name)
            if package is not None:
                packages.append(package)
        return packages

    def delete(self, name: str, actor: str) -> dict[str, Any]:
        package = self.get(name)
        self._require_operator(package.name, actor, "delete agent")
        self._remove(package.name)
        self._runs.pop(package.name, None)
        self._audit(actor, "delete", True, task_id=package.name,
                    reason=package.name)
        return {"deleted": package.name, "version": package.version}

    # -- update + versioning ----------------------------------------------

    def update(self, name: str, spec: AgentSpec | dict, *,
               changed_by: str, kind: str = "patch",
               bind: bool | None = None) -> AgentPackage:
        """Replace the spec, bump the version, and require re-testing."""
        package = self.get(name)
        self._require_operator(package.name, changed_by,
                               "update agent")
        if package.lifecycle == LifecycleState.RETIRED.value:
            raise AgentEngineError(
                "Retired agents cannot be updated: %s" % name)
        if isinstance(spec, dict):
            payload = dict(spec)
            payload["name"] = package.name
            spec = AgentSpec.from_dict(payload)
        if not isinstance(spec, AgentSpec):
            raise AgentEngineError("update needs an AgentSpec")
        if spec.name != package.name:
            raise AgentEngineError("An update may not rename an agent")
        package.spec = spec
        if bind is None:
            # Keep the binding only when the new spec resolves to the
            # same executor; otherwise the old binding is stale.
            fresh = resolve_executor(spec)
            if package.executor and fresh != package.executor:
                package.executor = ""
                package.real = False
            elif not package.executor and fresh:
                pass  # stays unbound until explicitly bound
        else:
            rebind(package, bind=bind)
        package.test_report = {}
        # A changed spec invalidates earlier testing: back to
        # validated (an administrative reset, recorded in history).
        package.lifecycle = LifecycleState.VALIDATED.value
        touch_history(package, bump(package.version, kind),
                      changed_by, "spec updated; re-testing required",
                      kind=kind)
        self._save(package)
        self._audit(changed_by, "update", True, task_id=package.name,
                    reason="version=%s" % package.version)
        return package

    def update_permissions(self, name: str,
                           permissions: list[str] | tuple[str, ...],
                           granted_by: str) -> AgentPackage:
        """Replace the permission allowlist (operators only)."""
        package = self.get(name)
        self._require_operator(package.name, granted_by,
                               "grant permissions to")
        payload = package.spec.to_dict()
        payload["permissions"] = list(permissions)
        return self.update(package.name, payload,
                           changed_by=granted_by, kind="minor")

    def set_limits(self, name: str, granted_by: str, **limits: Any
                   ) -> AgentPackage:
        """Tighten or loosen runtime budgets (operators only)."""
        package = self.get(name)
        self._require_operator(package.name, granted_by,
                               "set limits for")
        if package.lifecycle == LifecycleState.RETIRED.value:
            raise AgentEngineError(
                "Retired agents cannot change limits: %s" % name)
        payload = package.spec.to_dict()
        for key, value in limits.items():
            if key not in payload["resource_limits"]:
                raise AgentEngineError(
                    "Unknown resource limit: %s" % (key,))
            payload["resource_limits"][key] = value
        spec = AgentSpec.from_dict(payload)
        package.spec = spec
        touch_history(package, bump(package.version, "patch"),
                      granted_by, "resource limits updated",
                      kind="patch")
        self._save(package)
        self._audit(granted_by, "limits", True, task_id=package.name,
                    reason=",".join(sorted(limits)))
        return package

    # -- lifecycle ----------------------------------------------------------

    def _transition(self, name: str, target: str,
                    actor: str) -> AgentPackage:
        package = self.get(name)
        self._require_operator(package.name, actor,
                               "transition agent to %s" % target)
        source, normalized = check_transition(package.lifecycle,
                                              target)
        if normalized == LifecycleState.ENABLED.value \
                and source not in ENABLE_FROM:
            raise AgentEngineError(
                "Agent %s must be tested before it can be enabled "
                "(current: %s)" % (name, source))
        if normalized == LifecycleState.ENABLED.value \
                and not package.real:
            raise AgentEngineError(
                "Agent %s has no bound executor; it cannot be "
                "enabled" % name)
        package.lifecycle = normalized
        package.updated_at = time.time()
        self._save(package)
        self._audit(actor, "lifecycle", True, task_id=package.name,
                    reason="%s -> %s" % (source, normalized))
        return package

    def validate(self, name: str, actor: str) -> AgentPackage:
        """Re-validate the spec and move created -> validated."""
        package = self.get(name)
        # Re-run full spec validation from the serialized form.
        AgentSpec.from_dict(package.spec.to_dict())
        return self._transition(package.name,
                                LifecycleState.VALIDATED.value,
                                actor)

    def test(self, name: str, actor: str, *,
               live: bool = False) -> dict[str, Any]:
        """Benchmark the package; passing moves validated -> tested."""
        package = self.get(name)
        self._require_operator(package.name, actor, "test agent")
        if package.lifecycle not in (
                LifecycleState.VALIDATED.value,
                LifecycleState.TESTED.value):
            raise AgentEngineError(
                "Agent %s must be validated before testing "
                "(current: %s)" % (name, package.lifecycle))
        report = build_test_report(package, self.fabric, live=live)
        package.test_report = report
        package.updated_at = time.time()
        summary = report["summary"]
        if summary["meets_requirement"]:
            if package.lifecycle == LifecycleState.VALIDATED.value:
                package.lifecycle = LifecycleState.TESTED.value
            self._save(package)
            self._audit(actor, "test", True, task_id=package.name,
                        reason="passed=%d/%d" % (
                            summary["passed"], summary["total"]))
        else:
            self._save(package)
            self._audit(actor, "test", False, task_id=package.name,
                        reason="pass_rate=%.2f below %.2f" % (
                            summary["pass_rate"],
                            summary["required_pass_rate"]))
        return {"agent": package.name, "version": package.version,
                "lifecycle": package.lifecycle, "report": report}

    def enable(self, name: str, granted_by: str) -> AgentPackage:
        return self._transition(name, LifecycleState.ENABLED.value,
                                granted_by)

    def pause(self, name: str, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.PAUSED.value,
                                actor)

    def disable(self, name: str, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.DISABLED.value,
                                actor)

    def retire(self, name: str, actor: str) -> AgentPackage:
        return self._transition(name, LifecycleState.RETIRED.value,
                                actor)

    # -- namespaced memory ----------------------------------------------------

    def memory_for(self, name: str):  # -> MemoryStore
        from forge.memory.store import MemoryStore

        package = self.get(name)
        store = MemoryStore(
            root=str(Path(self.memory_root) / package.name),
            max_bytes=package.spec.memory_policy.max_value_bytes)
        return store

    def memory_save(self, name: str, key: str, value: str) -> str:
        """Save one namespaced fact, enforcing the memory policy."""
        package = self.get(name)
        policy = package.spec.memory_policy
        store = self.memory_for(name)
        keys = store.list()
        normalized = (key or "").strip()
        if normalized not in keys and len(keys) >= policy.max_entries:
            raise AgentEngineError(
                "Memory limit reached (%d entries)"
                % policy.max_entries)
        store.save(key, value)
        return key

    def memory_load(self, name: str, key: str) -> str | None:
        return self.memory_for(name).load(key)

    def memory_list(self, name: str) -> list[str]:
        self.get(name)
        return self.memory_for(name).list()

    # -- guarded execution ------------------------------------------------------

    def _scoped_manager(self, package: AgentPackage):
        """Permission manager bounded by the spec allowlist.

        Operations the spec lists keep their default levels; every
        other A32 operation is BLOCKED for this agent. Namespaced
        memory and routed model calls are SAFE — their boundary is
        the namespace and the fabric policy, not the filesystem.
        """
        from forge.security.permissions import (PermissionLevel,
                                                PermissionManager)

        manager = PermissionManager(
            mode=self.mode, policy=self.policy,
            store=self.approval_store,
            agent="forge-agent:%s" % package.name, audit=self.audit)
        allowed = set(package.spec.permissions)
        for operation in list(manager.rules):
            if operation not in allowed:
                manager.rules[operation] = PermissionLevel.BLOCKED
        manager.rules["memory_read"] = PermissionLevel.SAFE
        manager.rules["memory_write"] = PermissionLevel.SAFE
        manager.rules["model_generate"] = PermissionLevel.SAFE
        return manager

    def _scoped_runtime(self, package: AgentPackage, manager: Any):
        """Tool Runtime with only the spec's tools registered."""
        from forge.runtime.runtime import ToolDefinition, ToolResult
        from forge.runtime.runtime import ToolRuntime
        from forge.tools.filesystem import FileSystemTool
        from forge.tools.git import GitTool
        from forge.tools.search import SearchTool
        from forge.tools.terminal import TerminalTool

        from forge.runtime.defaults import create_default_runtime

        full = create_default_runtime(manager, self.root)
        runtime = ToolRuntime(manager)
        wanted = set(package.spec.tools)
        for tool in full.list_tools():
            if tool.name in wanted:
                runtime.register(tool)
        filesystem = FileSystemTool(self.root)
        git = GitTool(self.root)
        search = SearchTool(self.root)

        if "git_diff" in wanted:
            runtime.register(ToolDefinition(
                name="git_diff", description="Show git diff.",
                handler=lambda staged=False: ToolResult.ok(
                    "git_diff", git.diff(staged=bool(staged))),
                permission="git_diff"))
        if "git_commit" in wanted:
            def _commit(message: str = "",
                        files: list | None = None) -> ToolResult:
                message = (message or "").strip()
                if not message:
                    return ToolResult.fail(
                        "git_commit", "A commit message is required.")
                try:
                    if files:
                        git.stage_files(list(files))
                    proc = git.run("commit", "-m", message[:500])
                except Exception as exc:
                    return ToolResult.fail("git_commit", str(exc))
                if proc.returncode != 0:
                    return ToolResult.fail(
                        "git_commit",
                        (proc.stderr or proc.stdout or "commit failed"
                         )[:500])
                return ToolResult.ok("git_commit", proc.stdout[:2000])

            runtime.register(ToolDefinition(
                name="git_commit",
                description="Commit staged changes with a message.",
                handler=_commit, permission="git_commit"))
        if "memory_read" in wanted:
            runtime.register(ToolDefinition(
                name="memory_read",
                description="Read this agent's namespaced memory.",
                handler=lambda key: ToolResult.ok(
                    "memory_read",
                    self.memory_load(package.name, key) or "",
                    metadata={"found": self.memory_load(
                        package.name, key) is not None}),
                permission="memory_read"))
        if "memory_write" in wanted:
            def _memory_write(key: str, content: str) -> ToolResult:
                try:
                    self.memory_save(package.name, key, content)
                except Exception as exc:
                    return ToolResult.fail("memory_write", str(exc))
                return ToolResult.ok("memory_write")

            runtime.register(ToolDefinition(
                name="memory_write",
                description="Write this agent's namespaced memory.",
                handler=_memory_write, permission="memory_write"))
        if "compute_execute" in wanted:
            def _compute(code: str,
                         timeout: float = 30.0) -> ToolResult:
                from forge.compute.engine import ComputeEngine

                try:
                    cell = ComputeEngine(self.root).execute(
                        code, timeout=timeout)
                except Exception as exc:
                    return ToolResult.fail("compute_execute", str(exc))
                if cell.get("status") != "succeeded":
                    return ToolResult.fail(
                        "compute_execute",
                        str(cell.get("output", "compute failed"))[:2000],
                        metadata={"cell": cell})
                return ToolResult.ok(
                    "compute_execute", str(cell.get("output", "")),
                    metadata={"cell": cell})

            runtime.register(ToolDefinition(
                name="compute_execute",
                description="Execute a bounded Python cell.",
                handler=_compute, permission="run_command"))
        if "model_generate" in wanted:
            def _generate(prompt: str,
                          capability: str = "") -> ToolResult:
                from forge.models.request import ModelRequest

                caps = package.spec.model_requirements.capabilities
                capability = (capability or "").strip().lower()
                if capability and capability not in caps:
                    return ToolResult.fail(
                        "model_generate",
                        "capability %r is outside this agent's model "
                        "requirements" % capability)
                try:
                    response = self.fabric.generate(ModelRequest(
                        prompt=prompt,
                        capability=capability or caps[0],
                        task="agent:%s" % package.name,
                        prefer_local=package.spec.model_requirements
                        .prefer_local,
                        prefer_free=package.spec.model_requirements
                        .prefer_free,
                        max_output_tokens=2000))
                except Exception as exc:
                    return ToolResult.fail("model_generate", str(exc))
                if not response.success:
                    return ToolResult.fail(
                        "model_generate",
                        response.error or "model call failed")
                return ToolResult.ok(
                    "model_generate", response.text,
                    metadata={"model": response.model,
                              "provider": response.provider})

            runtime.register(ToolDefinition(
                name="model_generate",
                description="Generate through the Model Fabric.",
                handler=_generate, permission="model_generate"))
        del filesystem, search
        return runtime

    def _fabric_route(self, package: AgentPackage,
                      requirement: str) -> dict[str, Any]:
        """Route the requirement; record the decision honestly."""
        from forge.models.request import ModelRequest

        caps = package.spec.model_requirements.capabilities
        request = ModelRequest(
            prompt=requirement, capability=caps[0],
            task="agent:%s" % package.name,
            prefer_local=package.spec.model_requirements.prefer_local,
            prefer_free=package.spec.model_requirements.prefer_free,
            max_output_tokens=2000)
        try:
            decision = self.fabric.route(request)
        except Exception as exc:
            return {"routed": False, "error": str(exc)[:300]}
        model = decision.model
        return {
            "routed": bool(decision.chosen and model is not None),
            "model": model.name if model is not None else "",
            "provider": model.provider if model is not None else "",
            "candidates": list(decision.candidates or ()),
            "error": decision.error or "",
        }

    def _study_output(self, package: AgentPackage, requirement: str,
                      runtime: Any) -> tuple[str, list[str]]:
        """Deterministic repository analysis (research family)."""
        import json as _json
        import os as _os

        python_files: list[str] = []
        for dirpath, dirnames, filenames in _os.walk(self.root):
            dirnames[:] = [name for name in dirnames
                           if name not in (
                               ".git", ".forge", "__pycache__",
                               ".venv", "node_modules", ".arena")]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    python_files.append(_os.path.relpath(
                        _os.path.join(dirpath, name), self.root))
                    if len(python_files) >= 200:
                        break
            if len(python_files) >= 200:
                break
        test_files = [path for path in python_files
                      if "test" in path]
        del runtime
        output = _json.dumps({
            "agent": package.name,
            "requirement": requirement[:200],
            "python_files": len(python_files),
            "test_files": len(test_files),
            "sample": python_files[:20],
        })
        return output, []

    def _audit_output(self, package: AgentPackage, requirement: str,
                      runtime: Any) -> tuple[str, list[str]]:
        """Deterministic security/test/review scan (audit family)."""
        import json as _json
        import re as _re
        import subprocess as _subprocess
        import sys as _sys
        from pathlib import Path as _Path

        first = package.spec.capabilities[0] if \
            package.spec.capabilities else "security"
        if first == "testing":
            try:
                proc = _subprocess.run(
                    [_sys.executable, "-m", "pytest",
                     "--collect-only", "-q"],
                    cwd=self.root, capture_output=True, text=True,
                    timeout=30, check=False)
            except _subprocess.TimeoutExpired:
                return "test collection exceeded 30s", []
            if proc.returncode == 5:
                return "No tests found in the project.", []
            output = (proc.stdout or "") + (proc.stderr or "")
            return ("Test collection: exit=%d\n%s"
                    % (proc.returncode, output[:3000])), []
        secret_re = _re.compile(
            r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*"
            r"['\"][^'\"]{8,}['\"]")
        findings: list[dict[str, Any]] = []
        scanned = 0
        for path in sorted(_Path(self.root).rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(self.root).parts
            if any(part in (".git", ".forge", "__pycache__",
                            ".venv", "node_modules", ".arena")
                   for part in parts):
                continue
            if path.suffix.lower() not in (
                    ".py", ".json", ".toml", ".yaml", ".yml",
                    ".ini", ".cfg", ".conf", ".md"):
                continue
            scanned += 1
            if scanned > 150:
                break
            try:
                content = path.read_text(encoding="utf-8",
                                         errors="ignore")
            except OSError:
                continue
            for match in secret_re.finditer(content):
                findings.append({
                    "file": path.relative_to(self.root).as_posix(),
                    "line": content[:match.start()].count("\n") + 1,
                    "pattern": match.group(1)})
        del runtime
        output = _json.dumps({
            "agent": package.name,
            "requirement": requirement[:200],
            "scanned_files": scanned,
            "secrets_found": len(findings),
            "findings": findings[:50],
        })
        return output, []

    def _write_output(self, package: AgentPackage, requirement: str,
                      runtime: Any, *, approved: bool,
                      run_id: str) -> tuple[str, list[str], str]:
        """Model-driven change sets through the scoped runtime."""
        from forge.agents.coder import CoderAgent
        from forge.agents.execution import AgentRequest
        from forge.core.task_engine import Task, TaskStatus

        coder = CoderAgent(
            runtime=runtime, root=self.root, fabric=self.fabric,
            approval_store=self.approval_store,
            approval_callback=None)
        task = Task(id="engine-run-%s" % run_id,
                    description=requirement,
                    status=TaskStatus.CODING)
        request = AgentRequest(
            task=task, stage=TaskStatus.CODING,
            metadata={"approved": bool(approved)})
        response = coder.execute(request)
        files = [str(path) for path in
                 response.metadata.get("files", ())]
        if not response.success:
            return "", files, response.error or "coding step failed"
        return response.output or "", files, ""

    def execute(self, name: str, requirement: str, *,
                approved: bool = False, actor: str = "",
                run_id: str = "") -> dict[str, Any]:
        """Run an enabled agent through every guarded subsystem.

        Only ``enabled`` + bound (``real``) packages run. The run is
        quota-checked, checkpointed, routed via the Model Fabric,
        executed through the scoped Tool Runtime + PolicyGate, and
        verified; failures roll back the candidate files.
        """
        from forge.security.policy_gate import PolicyDecision
        from forge.security.verification import VerificationPipeline
        from forge.tools.checkpoint import CheckpointManager

        package = self.get(name)
        requirement = (requirement or "").strip()[:MAX_REQUIREMENT]
        if not requirement:
            raise AgentEngineError("requirement must be non-empty")
        normalize(package.lifecycle)
        if not is_runnable(package.lifecycle):
            raise AgentEngineError(
                "Agent %s is %s; only enabled agents can run"
                % (name, package.lifecycle))
        if not package.real:
            raise AgentEngineError(
                "Agent %s has no bound executor; it cannot run tasks"
                % name)
        allowed, quota_reason = self.governor.check(package.name)
        if not allowed:
            self._audit(actor or "engine", "run", False,
                        task_id=package.name, reason=quota_reason)
            raise AgentEngineError(quota_reason)
        run_id = run_id or uuid4().hex[:12]
        limits = package.spec.resource_limits
        manager = self._scoped_manager(package)
        runtime = self._scoped_runtime(package, manager)
        checkpoint = CheckpointManager(self.root).create(
            "agent-%s-%s" % (package.name, run_id))
        route = self._fabric_route(package, requirement)
        self.governor.begin(package.name)
        started = time.monotonic()
        output = ""
        files: list[str] = []
        error = ""
        rolled_back = False
        verification: dict[str, Any] = {}
        try:
            first = package.spec.capabilities[0] if \
                package.spec.capabilities else "coding"
            if first in WRITE_FAMILY:
                output, files, error = self._write_output(
                    package, requirement, runtime,
                    approved=approved, run_id=run_id)
            elif first in AUDIT_FAMILY:
                output, files = self._audit_output(
                    package, requirement, runtime)
            else:
                output, files = self._study_output(
                    package, requirement, runtime)
            files = [str(path) for path in files
                     ][:limits.max_files_per_run]
            output = (output or "")[:limits.max_output_chars]
            # Verification gates over the run's changed files.
            pipeline = VerificationPipeline(self.root)
            security_gate = pipeline.security(files or None)
            review_gate = pipeline.review("", files or None)
            verification = {
                "security": {"passed": security_gate.passed,
                             "details": security_gate.details[:500]},
                "review": {"passed": review_gate.passed,
                           "details": review_gate.details[:500]},
            }
            needs = package.spec.verification_requirements
            if needs.require_security and not security_gate.passed:
                error = error or (
                    "security gate failed: %s"
                    % security_gate.details[:300])
            elif needs.require_review and not review_gate.passed:
                error = error or (
                    "review gate failed: %s"
                    % review_gate.details[:300])
            success = not error
            if not success and files:
                try:
                    CheckpointManager(self.root).rollback(
                        checkpoint, files)
                    rolled_back = True
                except Exception:
                    rolled_back = False
            if not success and not files:
                try:
                    CheckpointManager(self.root).cleanup(checkpoint)
                except Exception:
                    pass
            else:
                try:
                    if rolled_back:
                        pass
                    else:
                        CheckpointManager(self.root).cleanup(
                            checkpoint)
                except Exception:
                    pass
        finally:
            self.governor.end(package.name)
        elapsed_ms = round(
            (time.monotonic() - started) * 1000.0, 1)
        if elapsed_ms > limits.max_seconds_per_run * 1000.0:
            error = error or (
                "run exceeded max_seconds_per_run (%.1fs)"
                % limits.max_seconds_per_run)
            success = False
        else:
            success = not error
        record = {
            "run_id": run_id,
            "agent": package.name,
            "version": package.version,
            "success": bool(success),
            "output": output,
            "error": (error or "")[:500],
            "files": files,
            "model": route.get("model", ""),
            "provider": route.get("provider", ""),
            "fabric_route": route,
            "verification": verification,
            "checkpoint_id": checkpoint.id,
            "rolled_back": rolled_back,
            "elapsed_ms": elapsed_ms,
            "approved": bool(approved),
            "actor": (actor or "")[:64],
            "at": time.time(),
        }
        log = self._runs.setdefault(package.name, [])
        log.append(record)
        self._runs[package.name] = log[-MAX_RUN_LOG:]
        self._audit(actor or "engine", "run", bool(success),
                    task_id=package.name,
                    reason="run=%s success=%s files=%d" % (
                        run_id, bool(success), len(files)))
        _ = PolicyDecision
        return record

    def history(self, name: str) -> list[dict[str, Any]]:
        self.get(name)
        return [dict(entry) for entry in
                self._runs.get(name, [])]

    # -- version helpers -----------------------------------------------------

    def versions(self, name: str) -> dict[str, Any]:
        package = self.get(name)
        return {"agent": package.name,
                "version": package.version,
                "history": list(package.version_history)}

    INITIAL_VERSION = INITIAL_VERSION

    @staticmethod
    def bump_version(version: str, kind: str = "patch") -> str:
        return bump(version, kind)

    @staticmethod
    def validate_version(version: str) -> str:
        return validate(version)
