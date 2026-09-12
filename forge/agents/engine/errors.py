"""Typed failures for the Agent Creation Engine (A81).

Every refusal in the engine raises one of these so callers (CLI,
desktop, control plane, tests) can branch on the cause instead of
parsing messages. All of them are ordinary exceptions — the engine
never swallows a refusal into a "partial success".
"""
from __future__ import annotations


class AgentEngineError(Exception):
    """Base class for every Agent Creation Engine failure."""


class AgentSpecError(AgentEngineError):
    """The specification is invalid or internally inconsistent."""

    def __init__(self, message: str, findings: tuple = ()) -> None:
        super().__init__(message)
        self.findings = tuple(findings)


class AgentNotFoundError(AgentEngineError):
    """No agent package exists under the requested name."""


class AgentExistsError(AgentEngineError):
    """An agent package already exists under the requested name."""


class AgentLifecycleError(AgentEngineError):
    """The requested lifecycle transition is not allowed."""


class AgentPermissionError(AgentEngineError):
    """A permission request was refused.

    Raised for self-grant attempts, grants beyond the specification
    ceiling, and operations outside an agent's declared set. A refusal
    is never retried and never partially applied.
    """


class AgentIsolationError(AgentEngineError):
    """An agent reached for another agent's private state."""


class AgentLimitError(AgentEngineError):
    """A declared resource limit was reached."""


class AgentVersionError(AgentEngineError):
    """Version records are immutable; this mutation was refused."""


class AgentPackageError(AgentEngineError):
    """The on-disk package is missing, corrupt, or not a Forge package."""


class AgentBenchmarkError(AgentEngineError):
    """The benchmark suite could not be executed as requested."""
