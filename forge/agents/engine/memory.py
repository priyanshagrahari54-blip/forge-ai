"""Agent memory (A83): policy-bounded and namespace-isolated.

Each agent gets a private namespace inside the project
:class:`~forge.memory.store.MemoryStore`. The policy in the spec decides
how much may be kept (entries, entry size, TTL) and whether the agent may
also use the shared *project* namespace.

Isolation is structural, not advisory:

* Every key is resolved under ``agents/<agent>/`` (or
  ``agents/_project/``), and a key that would traverse out of it is
  refused by the underlying store.
* A read that names another agent as its owner raises
  :class:`~forge.agents.engine.errors.AgentIsolationError`. There is no
  scope, policy, or grant that exposes one agent's private namespace to
  another.
* ``scope="none"`` disables memory: remember/recall refuse instead of
  silently writing somewhere.
"""
from __future__ import annotations

import json
import time
from typing import Any

from forge.agents.engine.errors import (
    AgentIsolationError,
    AgentPermissionError,
)
from forge.agents.engine.spec import MemoryPolicy

AGENT_PREFIX = "agents"
PROJECT_NAMESPACE = "_project"
MAX_KEY = 96


class AgentMemory:
    """Memory handle for exactly one agent."""

    def __init__(self, store: Any, agent: str, policy: MemoryPolicy) -> None:
        self.store = store
        self.agent = agent
        self.policy = policy

    # -- key handling ----------------------------------------------------

    def _namespace(self, shared: bool) -> str:
        if shared:
            return "%s/%s" % (AGENT_PREFIX, PROJECT_NAMESPACE)
        return "%s/%s" % (AGENT_PREFIX, self.agent)

    def _key(self, key: str, *, shared: bool = False) -> str:
        candidate = (key or "").strip()
        if not candidate:
            raise ValueError("Memory key cannot be empty")
        if len(candidate) > MAX_KEY:
            raise ValueError(
                "Memory key must be at most %d characters" % MAX_KEY)
        if candidate.startswith("/") or "\\" in candidate or \
                candidate in (".", "..") or ".." in candidate.split("/"):
            raise ValueError(
                "Memory key must be a relative path without traversal: %r"
                % key)
        return "%s/%s" % (self._namespace(shared), candidate)

    def _guard(self) -> None:
        if self.policy.scope == "none":
            raise AgentPermissionError(
                "Agent %r has memory scope 'none'" % self.agent)

    def _shared_allowed(self) -> bool:
        return self.policy.scope == "project"

    # -- operations ------------------------------------------------------

    def remember(self, key: str, value: str, *, shared: bool = False) -> dict:
        """Store one entry, bounded by the spec's memory policy."""
        self._guard()
        if shared and not self._shared_allowed():
            raise AgentPermissionError(
                "Agent %r may not write the shared project namespace "
                "(memory.scope=%r)" % (self.agent, self.policy.scope))
        text = value if isinstance(value, str) else json.dumps(value,
                                                               default=str)
        encoded = text.encode("utf-8")
        if len(encoded) > self.policy.max_entry_bytes:
            raise AgentPermissionError(
                "Memory entry exceeds the %d-byte bound"
                % self.policy.max_entry_bytes)
        full = self._key(key, shared=shared)
        existing = [name for name in self.store.list()
                    if name.startswith(self._namespace(shared) + "/")]
        if full not in existing and len(existing) >= self.policy.max_entries:
            raise AgentPermissionError(
                "Memory limit reached (%d entries)" % self.policy.max_entries)
        payload = json.dumps({"value": text, "at": time.time()})
        self.store.save(full, payload)
        return {"key": key, "shared": shared, "bytes": len(encoded)}

    def recall(self, key: str, *, shared: bool = False,
               owner: str = "") -> Any:
        """Read one entry. Naming another owner is always refused."""
        self._guard()
        if owner and owner.strip().lower() != self.agent.strip().lower():
            raise AgentIsolationError(
                "Agent %r cannot read the memory of %r"
                % (self.agent, owner))
        if shared and not self._shared_allowed():
            raise AgentPermissionError(
                "Agent %r may not read the shared project namespace "
                "(memory.scope=%r)" % (self.agent, self.policy.scope))
        full = self._key(key, shared=shared)
        raw = self.store.load(full)
        if raw is None:
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            return None
        stored_at = float(entry.get("at", 0.0))
        if self.policy.ttl_seconds > 0 and \
                time.time() - stored_at > self.policy.ttl_seconds:
            self.store.delete(full)
            return None
        return entry.get("value")

    def forget(self, key: str, *, shared: bool = False) -> bool:
        self._guard()
        if shared and not self._shared_allowed():
            raise AgentPermissionError(
                "Agent %r may not modify the shared project namespace"
                % self.agent)
        full = self._key(key, shared=shared)
        removed = bool(self.store.delete(full))
        if removed:
            self._prune(shared)
        return removed

    def _prune(self, shared: bool) -> None:
        """Remove this agent's namespace directory once it is empty.

        Keeps a probe (or a retired agent) from leaving empty directory
        litter in the project's memory root. Only ever touches the agent's
        own namespace, and a failure to remove is not an error.
        """
        from pathlib import Path

        try:
            root = Path(getattr(self.store, "root", "")).resolve()
            namespace = (root / self._namespace(shared)).resolve()
            namespace.relative_to(root)
        except (ValueError, OSError, TypeError):
            return
        for directory in (namespace, namespace.parent):
            try:
                if directory.is_dir() and directory != root \
                        and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                return

    def keys(self, *, shared: bool = False) -> list:
        """Keys in this agent's own namespace (never another agent's)."""
        prefix = self._namespace(shared) + "/"
        return sorted(name[len(prefix):] for name in self.store.list()
                      if name.startswith(prefix))

    def usage(self) -> dict:
        """Entry count and bytes for the detail view / desktop."""
        private = self.keys()
        shared = self.keys(shared=True) if self._shared_allowed() else []
        return {"scope": self.policy.scope, "private_entries": len(private),
                "shared_entries": len(shared),
                "max_entries": self.policy.max_entries,
                "max_entry_bytes": self.policy.max_entry_bytes,
                "ttl_seconds": self.policy.ttl_seconds}
