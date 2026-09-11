"""The Agent Runtime (A81): every agent operates through the six core
platform services, and through nothing else.

An enabled agent's only execution surface is :class:`AgentRuntime`:

- **Model Fabric** — :meth:`call_model` routes through a
  :class:`forge.models.fabric.ModelFabric` (or a provided stand-in),
  requesting the spec's model requirements. Agents never touch models
  directly.
- **PolicyGate** — every tool call is evaluated by
  :class:`forge.security.policy_gate.PolicyGate` against the agent's
  granted permission set. Anything outside the grant is BLOCKED, and
  ``DENY`` can never be overridden.
- **Tool Runtime** — :meth:`use_tool` delegates to a
  :class:`forge.runtime.runtime.ToolRuntime` that only holds the tools
  declared in the specification; undeclared tools do not exist.
- **Memory** — :meth:`remember`/:meth:`recall` write through a
  :class:`forge.memory.store.MemoryStore` confined to the engine-owned
  namespace ``agents/<name>/``, bounded by the memory policy.
- **Verification** — :meth:`verify` runs the
  :class:`forge.security.verification.VerificationPipeline` over the
  workspace.
- **Checkpoints** — :meth:`checkpoint`/:meth:`rollback` use the
  :class:`forge.tools.checkpoint.CheckpointManager` so every change set
  is restorable.

Isolation and no-self-grant:

- The permission manager returns BLOCKED for every operation outside
  the granted set (never the default approval-required fallback).
- ``approver`` is supplied by the operator at construction; the agent
  API surface has no way to set or influence it. ``REQUIRE_APPROVAL``
  verdicts fail unless the external approver grants them.
- Resource limits are hard: exhausting any budget raises
  :class:`AgentLimitError` and the runtime refuses further work.
- ``enabled`` is decided by the manager from the store's lifecycle
  state; the runtime cannot flip it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from forge.agent_engine.errors import (
    AgentLimitError,
    LifecycleError,
    NotRunnableError,
)
from forge.agent_engine.lifecycle import parse_lifecycle
from forge.agent_engine.spec import (
    MUTATING_TOOLS,
    READ_PERMISSIONS,
    TOOL_PERMISSIONS,
    WRITE_PERMISSIONS,
    _is_safe_relative_path,
)
from forge.agent_engine.store import AgentManifest
from forge.security.permissions import PermissionLevel, PermissionManager
from forge.security.policy_gate import PolicyDecision, PolicyGate


class AgentPermissionManager(PermissionManager):
    """A permission manager scoped to exactly the agent's grant.

    Inherits every A32/A33 behavior (mode restrictions, policy
    tightening, token redemption, audit), but overrides the default
    fallback: operations outside the granted set are BLOCKED, not
    approval-required. Isolation by construction.
    """

    def __init__(self, granted: dict[str, PermissionLevel], *,
                 mode: str = "assisted", policy=None, audit=None,
                 agent: str = "") -> None:
        super().__init__(rules=dict(granted), mode=mode, policy=policy,
                         audit=audit, agent=agent)

    def check(self, operation: str) -> PermissionLevel:
        if operation in self.rules:
            return self.rules[operation]
        return PermissionLevel.BLOCKED


def _granted_levels(permissions: tuple[str, ...]) -> dict[str, PermissionLevel]:
    """Levels for the agent's granted permissions.

    Reads run at SAFE; writes require approval; anything not granted is
    BLOCKED by the permission manager's override.
    """
    levels: dict[str, PermissionLevel] = {}
    for permission in permissions:
        if permission in READ_PERMISSIONS:
            levels[permission] = PermissionLevel.SAFE
        elif permission in WRITE_PERMISSIONS:
            levels[permission] = PermissionLevel.APPROVAL_REQUIRED
        else:
            levels[permission] = PermissionLevel.BLOCKED
    return levels


class AgentRuntime:
    """The complete, bounded execution surface of one agent version."""

    def __init__(
        self,
        manifest: AgentManifest,
        *,
        workspace: str | Path = ".",
        fabric: Any = None,
        tool_runtime: Any = None,
        memory: Any = None,
        verification: Any = None,
        checkpoints: Any = None,
        enabled: bool = False,
        approver: Callable[[str, dict[str, Any]], bool] | None = None,
        lifecycle: str = "",
    ) -> None:
        self.manifest = manifest
        self.spec = manifest.spec
        self.name = manifest.name
        self.version = manifest.version
        #: Live lifecycle (authoritative when supplied by the manager).
        self.lifecycle = lifecycle or manifest.lifecycle
        self.workspace = Path(workspace).resolve()
        self._fabric = fabric
        self._approver = approver
        self._enabled = bool(enabled)

        #: Operations the operator pre-approved for this session.
        self._permission_manager = AgentPermissionManager(
            _granted_levels(self.spec.permissions), agent=self.name)

        self._tool_runtime = tool_runtime
        if self._tool_runtime is None:
            self._tool_runtime = self._build_tool_runtime()

        self._memory = memory
        if self._memory is None:
            from forge.memory.store import MemoryStore

            self._memory = MemoryStore(str(self.workspace / ".forge" / "memory"),
                                       max_bytes=None)
        self._verification = verification
        if self._verification is None:
            from forge.security.verification import VerificationPipeline

            self._verification = VerificationPipeline(self.workspace)
        self._checkpoints = checkpoints
        if self._checkpoints is None:
            from forge.tools.checkpoint import CheckpointManager

            self._checkpoints = CheckpointManager(self.workspace)

        # -- resource accounting -----------------------------------------
        self._started = time.monotonic()
        self._requests = 0
        self._tokens_used = 0
        self._files_written: set[str] = set()
        self._memory_entries: set[str] = set()
        self._refused = False

    # -- construction ------------------------------------------------------

    #: Spec tool names that map onto a differently named default tool.
    _TOOL_IMPLEMENTATIONS: dict[str, str] = {"run_command": "terminal"}

    def _build_tool_runtime(self):
        """Build a ToolRuntime holding only this agent's declared tools."""
        from forge.runtime.defaults import create_default_runtime
        from forge.runtime.runtime import ToolDefinition, ToolRuntime

        defaults = create_default_runtime(self._permission_manager,
                                          str(self.workspace))
        runtime = ToolRuntime(self._permission_manager)
        for tool in self.spec.tools:
            implementation = self._TOOL_IMPLEMENTATIONS.get(tool, tool)
            definition = defaults.tools.get(implementation)
            if definition is None:
                raise LifecycleError(
                    f"tool {tool!r} declared in the specification has no "
                    f"runtime implementation")
            runtime.register(ToolDefinition(
                name=tool,  # the agent calls tools by their spec names
                description=definition.description,
                handler=definition.handler,
                permission=definition.permission,
            ))
        return runtime

    def _fabric_or_default(self):
        if self._fabric is not None:
            return self._fabric
        from forge.models.fabric import ModelFabric

        return ModelFabric.from_defaults()

    # -- gating --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _require_runnable(self) -> None:
        if self._refused:
            raise AgentLimitError(
                f"agent {self.name!r} exhausted a resource limit; no "
                f"further work is allowed")
        if not self._enabled:
            state = parse_lifecycle(self.lifecycle).value
            raise NotRunnableError(
                f"agent {self.name!r} is {state}, not enabled; it cannot "
                f"execute until the manager enables it")

    def _check_budget(self) -> None:
        wall = time.monotonic() - self._started
        limits = self.spec.limits
        if wall > limits.max_wall_seconds:
            self._refuse("wall-clock budget exhausted")
        if self._requests >= limits.max_requests:
            self._refuse("request budget exhausted")
        if len(self._files_written) > limits.max_files_written:
            self._refuse("file-write budget exhausted")

    def _refuse(self, reason: str) -> None:
        self._refused = True
        raise AgentLimitError(
            f"agent {self.name!r} {reason} "
            f"(limits: {self.spec.limits.to_dict() if hasattr(self.spec.limits, 'to_dict') else self.spec.limits})")

    # -- Model Fabric --------------------------------------------------------

    def call_model(self, prompt: str, *, capability: str = "",
                   max_output_tokens: int | None = None) -> dict[str, Any]:
        """Route one model request through the Model Fabric.

        The requested capability defaults to the spec's first model
        requirement; the fabric enforces capability availability and the
        spec's token budget caps every request.
        """
        self._require_runnable()
        self._check_budget()
        limits = self.spec.limits
        model_requirements = self.spec.model
        required = capability or (model_requirements.capabilities[0]
                                  if model_requirements.capabilities
                                  else self.spec.capabilities[0])
        prompt_text = str(prompt or "")
        estimated = max(1, len(prompt_text) // 4)
        if estimated > limits.max_tokens_per_request:
            # Per-request rejection: the request is refused, the session
            # stays usable.
            raise AgentLimitError(
                f"agent {self.name!r} prompt estimates {estimated} tokens, "
                f"above the {limits.max_tokens_per_request}-token "
                f"per-request budget")
        self._tokens_used += estimated

        from forge.models.request import ModelRequest

        request = ModelRequest(
            prompt=prompt_text,
            capability=required,
            required_capabilities=model_requirements.capabilities,
            min_context_window=model_requirements.min_context_tokens,
            max_output_tokens=max_output_tokens
            or min(limits.max_tokens_per_request, 8192),
        )
        self._requests += 1
        response = self._fabric_or_default().generate(request)
        payload = {
            "success": bool(response.success),
            "model": getattr(response, "model", "") or "",
            "provider": getattr(response, "provider", "") or "",
            "text": getattr(response, "text", "") or "",
            "error": getattr(response, "error", "") or "",
            "capability": required,
        }
        if (not self.spec.model.allow_fallback
                and not payload["success"]):
            payload["error"] = (payload["error"] or
                                "no non-fallback model available")
        return payload

    # -- Tool Runtime + PolicyGate -------------------------------------------

    def use_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """Execute one declared tool through PolicyGate + ToolRuntime.

        Isolation rules, in order:

        1. only tools declared in the specification exist;
        2. the PolicyGate evaluates the tool's permission operation
           against the agent's grant (non-granted ⇒ BLOCKED ⇒ DENY);
        3. ``REQUIRE_APPROVAL`` fails unless the external approver
           (supplied by the operator at construction) grants it;
        4. writes must land inside the spec's working directories;
        5. hard resource budgets are checked before and after.
        """
        self._require_runnable()
        self._check_budget()

        if tool_name not in self.spec.tools:
            return self._deny_tool(
                tool_name, f"tool {tool_name!r} is not granted to agent "
                f"{self.name!r} (isolation boundary)")

        operation = TOOL_PERMISSIONS[tool_name]
        path = kwargs.get("path") if isinstance(kwargs.get("path"), str) else ""

        # Writes are confined to the declared working directories.
        if (tool_name in MUTATING_TOOLS and path
                and not self._path_allowed_to_write(path)):
            return self._deny_tool(
                tool_name, f"path {path!r} is outside this agent's "
                f"working directories {list(self.spec.limits.working_dirs)}")

        # File-write budget: one new path per distinct write target.
        if tool_name in MUTATING_TOOLS and path:
            if (len(self._files_written) >= self.spec.limits.max_files_written
                    and path not in self._files_written):
                self._refuse("file-write budget exhausted")

        gate = PolicyGate(self._permission_manager)
        outcome = gate.evaluate(
            operation=operation, path=path, tool=tool_name,
            risk=str(kwargs.get("risk") or "NONE"),
            agent=self.name, approved=False)

        if outcome.decision == PolicyDecision.DENY:
            return self._deny_tool(tool_name, outcome.reason)
        if outcome.decision == PolicyDecision.REQUIRE_APPROVAL:
            if self._approver is not None and self._approver(
                    operation, {"tool": tool_name, "path": path,
                                "kwargs": dict(kwargs)}):
                pass  # operator approval, never agent self-approval
            else:
                return self._deny_tool(tool_name, outcome.reason)

        result = self._tool_runtime.execute(
            tool_name, approved=self._approver is not None,
            actor=self.name, task_id="", **kwargs)

        if (getattr(result, "success", False) and tool_name in MUTATING_TOOLS
                and path):
            self._files_written.add(path)
        if not getattr(result, "success", False) and self._requests == 0:
            pass  # tool failures are results, not budget events
        return result

    def _deny_tool(self, tool_name: str, reason: str) -> Any:
        from forge.runtime.runtime import ToolResult

        return ToolResult.fail(tool_name, reason or
                               "denied by the permission boundary")

    def _path_allowed_to_write(self, path: str) -> bool:
        if not _is_safe_relative_path(path):
            return False
        working_dirs = self.spec.limits.working_dirs
        if not working_dirs:
            return True  # repo-wide; protected paths still DENY via PolicyGate
        parts = path.split("/")
        return any(
            parts[:len(workdir.split("/"))] == workdir.split("/")
            for workdir in working_dirs)

    # -- Memory ---------------------------------------------------------------

    def _memory_namespace(self) -> str:
        scope = "shared" if self.spec.memory.retention == "forever" \
            else f"v{self.version}"
        return f"agents/{self.name}/{scope}"

    def _memory_key(self, key: str) -> str:
        if not _is_safe_relative_path(key):
            raise AgentLimitError(
                f"memory key {key!r} must be a relative path without "
                f"traversal")
        return f"{self._memory_namespace()}/{key}"

    def remember(self, key: str, content: str) -> None:
        """Store one bounded memory entry in the agent's own namespace."""
        self._require_runnable()
        if not self.spec.memory.enabled:
            raise AgentLimitError(
                f"agent {self.name!r} has memory disabled by policy")
        content_text = str(content)
        if len(content_text.encode("utf-8")) > self.spec.memory.max_entry_bytes:
            raise AgentLimitError(
                f"memory entry exceeds the {self.spec.memory.max_entry_bytes}"
                f"-byte bound")
        if (key not in self._memory_entries
                and len(self._memory_entries) >= self.spec.memory.max_entries):
            raise AgentLimitError(
                f"memory entry budget exhausted "
                f"({self.spec.memory.max_entries} entries)")
        self._memory.save(self._memory_key(key), content_text)
        self._memory_entries.add(key)

    def recall(self, key: str) -> str | None:
        """Read one entry from the agent's own namespace (None if absent)."""
        self._require_runnable()
        if not self.spec.memory.enabled:
            return None
        return self._memory.load(self._memory_key(key))

    def forget(self, key: str) -> bool:
        self._require_runnable()
        deleted = self._memory.delete(self._memory_key(key))
        if deleted:
            self._memory_entries.discard(key)
        return deleted

    # -- Verification & Checkpoints -------------------------------------------

    def verify(self, changed_files: list[str] | None = None,
               diff: str = "") -> dict[str, Any]:
        """Run the repository verification pipeline and report every gate."""
        self._require_runnable()
        result = self._verification.run(diff=diff,
                                        changed_files=changed_files or [])
        gates = []
        for gate in getattr(result, "gates", []):
            gates.append({
                "name": getattr(gate, "name", "?"),
                "passed": bool(getattr(gate, "passed", False)),
                "details": getattr(gate, "details", ""),
            })
        return {"passed": bool(getattr(result, "passed", False)),
                "gates": gates,
                "changed_files": list(changed_files or [])}

    def checkpoint(self, label: str = "agent-change",
                   declared: list[str] | None = None) -> Any:
        self._require_runnable()
        return self._checkpoints.create(label=label, declared=declared or [])

    def rollback(self, checkpoint: Any,
                 changed_files: list[str] | None = None) -> None:
        self._require_runnable()
        self._checkpoints.rollback(checkpoint, changed_files=changed_files)

    # -- introspection -----------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Resource usage report (also what CLI/desktop render)."""
        return {
            "agent": self.name,
            "version": self.version,
            "enabled": self._enabled,
            "workspace": str(self.workspace),
            "requests": self._requests,
            "tokens_used": self._tokens_used,
            "files_written": sorted(self._files_written),
            "memory_entries": len(self._memory_entries),
            "refused": self._refused,
            "limits": {
                "max_tokens_per_request":
                    self.spec.limits.max_tokens_per_request,
                "max_requests": self.spec.limits.max_requests,
                "max_wall_seconds": self.spec.limits.max_wall_seconds,
                "max_files_written": self.spec.limits.max_files_written,
                "working_dirs": list(self.spec.limits.working_dirs),
            },
        }
