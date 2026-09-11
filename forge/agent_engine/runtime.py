"""Bound agent runtime (A81): the only way a created agent acts.

:class:`BoundAgent` wires a package to the real Forge subsystems:

* **Model Fabric** — every model call is a :class:`ModelRequest` built
  from the package's routing plan; the agent cannot pick a provider.
* **PolicyGate** — every tool call is authorized *before* it happens,
  with the operation, path, tool, and risk visible.
* **Tool Runtime** — the only path from an authorized decision to a
  real side effect.
* **Memory** — namespaced, bounded, TTL'd, and secret-scrubbed.
* **Verification** — the declared gates run before work is accepted.
* **Checkpoints** — taken before the first write, restored on failure.

Isolation invariants enforced here (and tested):

1. An agent can only touch paths inside its declared scopes.
2. An agent can only use tools its package declared.
3. An agent can never grant itself a permission: :meth:`request_grant`
   always refuses, and the gate is consulted for every action.
4. Memory keys are namespaced per agent; agent A cannot read agent B.
5. Resource limits are enforced before the side effect, not after.
"""
from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Any

from forge.agent_engine.lifecycle import RUNNABLE_STATES

#: Patterns that must never appear in agent memory or model context.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9]{16,}"),
)

REDACTED = "[redacted]"


class AgentRuntimeError(RuntimeError):
    """A bound agent refused or failed an action, with a reason."""


def redact(text: str) -> str:
    output = str(text)
    for pattern in _SECRET_PATTERNS:
        output = pattern.sub(REDACTED, output)
    return output


def contains_secret(text: str) -> bool:
    return any(pattern.search(str(text)) for pattern in _SECRET_PATTERNS)


def path_allowed(path: str, scopes) -> bool:
    """True when *path* matches one of the declared glob scopes."""
    candidate = str(path or "").strip()
    if not candidate:
        return False
    if candidate.startswith("/") or "\\" in candidate:
        return False
    parts = candidate.split("/")
    if ".." in parts or ".git" in parts or ".forge" in parts:
        return False
    for scope in scopes or ():
        if scope == "**":
            return True
        if fnmatch.fnmatch(candidate, scope):
            return True
        # `docs/**` should also match `docs/a/b.md` and `docs/x.md`.
        if scope.endswith("/**") and (
                candidate == scope[:-3]
                or candidate.startswith(scope[:-2])):
            return True
    return False


@dataclass
class AgentMemory:
    """Namespaced, bounded memory for one agent."""

    namespace: str
    max_entries: int = 32
    max_bytes: int = 64 * 1024
    ttl_seconds: float = 3600.0
    enabled: bool = True
    store: Any = None
    _entries: dict = field(default_factory=dict)

    def _key(self, key: str) -> str:
        clean = str(key or "").strip()
        if not clean or "/" in clean or ".." in clean or "\\" in clean:
            raise AgentRuntimeError(
                "Invalid memory key: {0!r}".format(key))
        return "{0}/{1}".format(self.namespace, clean)

    def save(self, key: str, value: str) -> str:
        if not self.enabled:
            raise AgentRuntimeError(
                "This agent's memory policy is 'none'; nothing may be "
                "stored.")
        full = self._key(key)
        text = str(value)
        if contains_secret(text):
            raise AgentRuntimeError(
                "Refusing to store secret-bearing content in agent "
                "memory.")
        encoded = len(text.encode("utf-8"))
        if encoded > self.max_bytes:
            raise AgentRuntimeError(
                "Memory entry exceeds the {0}-byte budget".format(
                    self.max_bytes))
        self._expire()
        if full not in self._entries and \
                len(self._entries) >= self.max_entries:
            raise AgentRuntimeError(
                "Memory entry limit reached ({0})".format(
                    self.max_entries))
        self._entries[full] = (text, time.time())
        if self.store is not None:
            self.store.save(full, text)
        return full

    def load(self, key: str):
        if not self.enabled:
            return None
        self._expire()
        entry = self._entries.get(self._key(key))
        return entry[0] if entry else None

    def keys(self) -> list:
        self._expire()
        return sorted(self._entries)

    def _expire(self) -> None:
        if self.ttl_seconds <= 0:
            return
        now = time.time()
        for key in [k for k, (_v, at) in self._entries.items()
                    if now - at > self.ttl_seconds]:
            del self._entries[key]


@dataclass
class AgentRun:
    """A record of one bounded agent action sequence."""

    agent: str
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    actions: list = field(default_factory=list)
    denials: list = field(default_factory=list)
    files_touched: list = field(default_factory=list)
    bytes_written: int = 0
    tokens: int = 0
    checkpoint_id: str = ""
    status: str = "running"

    def to_dict(self) -> dict:
        return {"agent": self.agent, "started_at": self.started_at,
                "ended_at": self.ended_at, "actions": list(self.actions),
                "denials": list(self.denials),
                "files_touched": list(self.files_touched),
                "bytes_written": self.bytes_written, "tokens": self.tokens,
                "checkpoint_id": self.checkpoint_id,
                "status": self.status}


class BoundAgent:
    """A package bound to live Forge subsystems."""

    def __init__(self, package, *, fabric=None, policy_gate=None,
                 tool_runtime=None, memory_store=None,
                 verification=None, checkpoints=None,
                 lifecycle=None) -> None:
        self.package = package
        self.spec = package.spec
        self.name = package.name
        self.fabric = fabric
        self.policy_gate = policy_gate
        self.tool_runtime = tool_runtime
        self.verification = verification
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        plan = package.runtime.get("memory", {})
        self.memory = AgentMemory(
            namespace=plan.get("namespace",
                               "agents/{0}".format(package.name)),
            max_entries=plan.get("max_entries", 32),
            max_bytes=plan.get("max_bytes", 64 * 1024),
            ttl_seconds=plan.get("ttl_seconds", 3600.0),
            enabled=bool(plan.get("enabled", True)),
            store=memory_store)
        self._runs: list = []
        self._active: int = 0
        self._run_starts: list = []

    # -- lifecycle -------------------------------------------------------

    @property
    def state(self) -> str:
        if self.lifecycle is not None:
            return self.lifecycle.state(self.name)
        return self.package.state

    def _require_runnable(self) -> None:
        if self.state not in RUNNABLE_STATES:
            raise AgentRuntimeError(
                "Agent {0!r} is {1}; only enabled agents may run.".format(
                    self.name, self.state))

    # -- the refusal that makes self-escalation impossible ---------------

    def request_grant(self, *_args: Any, **_kwargs: Any):
        """Always refuse. Agents never grant themselves permissions."""
        raise AgentRuntimeError(
            "Agents cannot grant permissions to themselves. An operator "
            "must change the specification and re-validate the agent.")

    # -- runs ------------------------------------------------------------

    def begin_run(self) -> AgentRun:
        self._require_runnable()
        limits = self.spec.resource_limits
        now = time.time()
        self._run_starts = [t for t in self._run_starts
                            if now - t < 3600.0]
        if len(self._run_starts) >= limits.max_runs_per_hour:
            raise AgentRuntimeError(
                "Agent {0!r} hit its hourly run limit ({1})".format(
                    self.name, limits.max_runs_per_hour))
        if self._active >= limits.max_concurrent_runs:
            raise AgentRuntimeError(
                "Agent {0!r} hit its concurrency limit ({1})".format(
                    self.name, limits.max_concurrent_runs))
        self._run_starts.append(now)
        self._active += 1
        run = AgentRun(agent=self.name)
        if self.checkpoints is not None and \
                self.package.runtime.get("checkpoints", {}).get("enabled"):
            checkpoint = self.checkpoints.create(
                "agent-{0}".format(self.name))
            run.checkpoint_id = getattr(checkpoint, "id", "")
            self._last_checkpoint = checkpoint
        self._runs.append(run)
        return run

    def end_run(self, run: AgentRun, status: str = "succeeded") -> dict:
        run.ended_at = time.time()
        run.status = status
        self._active = max(0, self._active - 1)
        return run.to_dict()

    def rollback(self, run: AgentRun) -> bool:
        checkpoint = getattr(self, "_last_checkpoint", None)
        if self.checkpoints is None or checkpoint is None:
            return False
        self.checkpoints.rollback(checkpoint, list(run.files_touched))
        return True

    # -- authorization ---------------------------------------------------

    def _grant_for(self, tool: str):
        for grant in self.package.runtime["tool_runtime"]["grants"]:
            if grant["tool"] == tool:
                return grant
        return None

    def authorize(self, tool: str, *, path: str = "",
                  risk: str = "NONE", approved: bool = False,
                  run: AgentRun = None) -> dict:
        """Authorize one action. Never bypasses the PolicyGate."""
        grant = self._grant_for(tool)
        if grant is None:
            decision = {"allowed": False, "decision": "DENY",
                        "reason": "Tool {0!r} is not part of this agent's "
                                  "package.".format(tool)}
            if run is not None:
                run.denials.append(decision)
            return decision

        scopes = grant["scopes"]
        if path and not path_allowed(path, scopes):
            decision = {"allowed": False, "decision": "DENY",
                        "reason": "Path {0!r} is outside this agent's "
                                  "declared scope.".format(path)}
            if run is not None:
                run.denials.append(decision)
            return decision

        limits = self.spec.resource_limits
        if grant["writes"] and run is not None:
            if path and path not in run.files_touched and \
                    len(run.files_touched) >= limits.max_files_touched:
                decision = {"allowed": False, "decision": "DENY",
                            "reason": "File budget exhausted ({0})".format(
                                limits.max_files_touched)}
                run.denials.append(decision)
                return decision

        if grant["requires_approval"] and not approved:
            decision = {"allowed": False, "decision": "REQUIRE_APPROVAL",
                        "reason": "This agent's writes require explicit "
                                  "approval."}
            if run is not None:
                run.denials.append(decision)
            return decision

        if self.policy_gate is not None:
            outcome = self.policy_gate.evaluate(
                operation=grant["operation"], path=path, tool=tool,
                risk=risk, approved=approved, agent=self.name)
            allowed = bool(getattr(outcome, "allowed", False))
            decision = {
                "allowed": allowed,
                "decision": str(getattr(
                    getattr(outcome, "decision", ""), "value",
                    getattr(outcome, "decision", ""))),
                "reason": getattr(outcome, "reason", ""),
            }
            if not allowed and run is not None:
                run.denials.append(decision)
            return decision

        return {"allowed": True, "decision": "ALLOW", "reason": ""}

    # -- acting ----------------------------------------------------------

    def use_tool(self, tool: str, *, path: str = "", risk: str = "NONE",
                 approved: bool = False, run: AgentRun = None,
                 **kwargs: Any):
        """Authorize, then execute through the Tool Runtime."""
        self._require_runnable()
        decision = self.authorize(tool, path=path, risk=risk,
                                  approved=approved, run=run)
        if not decision["allowed"]:
            raise AgentRuntimeError(decision["reason"])
        grant = self._grant_for(tool)
        if self.tool_runtime is None:
            raise AgentRuntimeError(
                "No tool runtime is bound to this agent.")
        call = dict(kwargs)
        if path:
            call["path"] = path
        content = call.get("content")
        if grant["writes"] and isinstance(content, str):
            limits = self.spec.resource_limits
            size = len(content.encode("utf-8"))
            written = (run.bytes_written if run else 0) + size
            if written > limits.max_bytes_written:
                raise AgentRuntimeError(
                    "Write budget exhausted ({0} bytes)".format(
                        limits.max_bytes_written))
        result = self.tool_runtime.execute(
            grant["operation"], approved=approved, actor=self.name,
            risk=risk, **call)
        if run is not None:
            run.actions.append({"tool": tool, "path": path,
                                "success": bool(
                                    getattr(result, "success", False))})
            if grant["writes"] and getattr(result, "success", False):
                if path and path not in run.files_touched:
                    run.files_touched.append(path)
                if isinstance(content, str):
                    run.bytes_written += len(content.encode("utf-8"))
        return result

    def think(self, prompt: str, *, context: str = "",
              run: AgentRun = None):
        """Route one model call through the Model Fabric."""
        self._require_runnable()
        if self.fabric is None:
            raise AgentRuntimeError(
                "No Model Fabric is bound to this agent.")
        from forge.models.request import ModelRequest

        plan = self.package.runtime["model_fabric"]
        request = ModelRequest(
            prompt="{0}\n\n{1}".format(self.package.prompt,
                                       redact(prompt)),
            capability=plan["capability"],
            context=redact(context),
            min_context_window=plan["min_context_window"],
            max_output_tokens=plan["max_output_tokens"],
            complexity=plan["complexity"],
            prefer_local=plan["prefer_local"],
            prefer_free=plan["prefer_free"],
            metadata={"agent": self.name,
                      "package": self.package.package_id()},
        )
        response = self.fabric.generate(request)
        if run is not None:
            run.actions.append({"tool": "model", "path": "",
                                "success": True})
            run.tokens += (int(getattr(response, "input_tokens", 0) or 0)
                           + int(getattr(response, "output_tokens", 0)
                                 or 0))
            limits = self.spec.resource_limits
            if run.tokens > limits.max_tokens_per_run:
                raise AgentRuntimeError(
                    "Token budget exhausted ({0})".format(
                        limits.max_tokens_per_run))
        return response

    def verify(self, changed_files=None):
        """Run the declared verification gates."""
        if self.verification is None:
            raise AgentRuntimeError(
                "No verification pipeline is bound to this agent.")
        gates = self.package.runtime["verification"]["gates"]
        results = {}
        for gate in gates:
            runner = getattr(self.verification, gate, None)
            if runner is None:
                results[gate] = {"passed": False,
                                 "details": "gate unavailable"}
                continue
            if gate in ("security", "review"):
                outcome = runner(changed_files or [])
            else:
                outcome = runner()
            results[gate] = {
                "passed": bool(getattr(outcome, "passed", False)),
                "details": str(getattr(outcome, "details", ""))[:500],
            }
        return {"gates": results,
                "passed": all(item["passed"] for item in results.values())}

    def runs(self) -> list:
        return [run.to_dict() for run in self._runs]
