"""Error hierarchy for the Agent Creation Engine (A81).

Every engine failure carries a specific exception type so callers (CLI,
desktop, tests) can distinguish an invalid specification from a forbidden
permission escalation, an illegal lifecycle transition, or an exhausted
resource budget.
"""
from __future__ import annotations


class AgentEngineError(Exception):
    """Base class for all Agent Creation Engine failures."""


class SpecError(AgentEngineError):
    """The agent specification is invalid or incomplete."""


class LifecycleError(AgentEngineError):
    """The requested lifecycle transition is not permitted."""


class NotRunnableError(LifecycleError):
    """The agent is not in a state that permits execution."""


class PermissionEscalationError(AgentEngineError):
    """A permission change would grant the agent new powers.

    Agents may never self-grant permissions. A permission set may only
    grow when a human operator explicitly confirms the escalation, and
    even then only through the factory (never through the runtime).
    """


class AgentLimitError(AgentEngineError):
    """A declared resource limit has been exhausted."""


class AgentNotFoundError(AgentEngineError):
    """The requested agent (or agent version) does not exist."""
