"""Tool capability registry (A84 Stage E1).

One descriptor per tool, describing what it can do and what it costs — the
vocabulary the planner reasons over:

``name`` / ``capability``
    Identity plus the primary fabric capability family it serves.
``input_schema`` / ``output_schema``
    Small declarative shapes (required keys, typed keys). The verifier uses
    them to detect malformed output; they are never sent to a model as
    executable authority.
``permissions``
    The A33 mapping (``resource`` + ``operation``) that gates execution.
    Registration does *not* imply permission: every real invocation still
    evaluates the policy engine, and ``DENY`` stays ``DENY``.
``risk_level``
    ``NONE``..``CRITICAL`` using the same ladder as A33 (fail-closed for
    unrecognized labels via :func:`forge.security.policy.risk_rank`).
``auth_required`` / ``cost`` / ``latency_ms``
    Operating facts. Unknown values stay ``unknown``/``0.0`` — the registry
    never invents a latency or a price.
``availability``
    Resolved honestly from runtime evidence: one of
    ``live`` / ``ready`` / ``configured`` / ``architecture`` / ``simulated``
    / ``blocked`` / ``missing`` (the capability-reality vocabulary). A probe
    may be supplied; with none, the tool reports ``architecture`` — the code
    exists, nothing more is claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from forge.security.policy import Resource, risk_rank

__all__ = ["ToolCapabilityRegistry", "ToolDescriptor", "ToolRisk",
           "builtin_registry"]


class ToolRisk(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @classmethod
    def values(cls) -> Tuple[str, ...]:
        return tuple(member.value for member in cls)

    @classmethod
    def parse(cls, value: Any) -> "ToolRisk":
        text = str(value or "NONE").strip().upper()
        try:
            return cls(text)
        except ValueError:
            # Unrecognized labels rank as HIGH (fail closed), mirroring the
            # A33 risk ladder — the *stored* value stays honest though.
            return cls.HIGH


#: Availability states mirror forge.capabilities.reality so the registry and
#: the capability-truth surface can never disagree about a word.
AVAILABILITY_STATES = ("live", "ready", "configured", "architecture",
                       "simulated", "blocked", "missing")


@dataclass(frozen=True)
class ToolDescriptor:
    """Everything Forge knows about one tool *without running it*."""

    name: str
    capability: str
    description: str = ""
    #: Fabric capability families this tool can satisfy (planner vocabulary).
    serves: Tuple[str, ...] = ()
    input_schema: Dict[str, str] = field(default_factory=dict)
    output_schema: Dict[str, str] = field(default_factory=dict)
    #: A33 gate: (resource, operation). Execution always re-checks policy.
    permission_resource: str = ""
    permission_operation: str = ""
    risk: str = "NONE"
    auth_required: bool = False
    #: USD per call when paid (0.0 free/unknown-free); never fabricated.
    cost_usd: float = 0.0
    #: Typical latency in ms from measured telemetry; 0.0 = no evidence.
    latency_ms: float = 0.0
    #: Name of the idempotency key when retry-safe (E3); "" = not retry-safe.
    idempotent: bool = False
    #: Whether results may go stale and must carry a retrieval timestamp.
    staleness_bound: bool = False
    tags: Tuple[str, ...] = ()

    # -- validation ----------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("tool name is required")
        if self.permission_resource:
            try:
                Resource(self.permission_resource)
            except ValueError:
                raise ValueError(
                    f"tool {self.name!r} names unknown A33 resource "
                    f"{self.permission_resource!r}") from None
        if self.permission_resource and not self.permission_operation:
            raise ValueError(
                f"tool {self.name!r}: permission resource needs an operation")
        if self.permission_operation:
            ops = _allowed_operations(self.permission_resource)
            if ops and self.permission_operation not in ops:
                raise ValueError(
                    f"tool {self.name!r}: operation "
                    f"{self.permission_operation!r} is not defined for "
                    f"resource {self.permission_resource!r}")
        ToolRisk.parse(self.risk)

    @property
    def risk_rank(self) -> int:
        return risk_rank(self.risk)

    @property
    def gated(self) -> bool:
        """True when execution must pass an A33 policy check."""
        return bool(self.permission_resource and self.permission_operation)

    def requires_approval_hint(self) -> bool:
        """Advisory: HIGH/CRITICAL tools should surface approval even when
        the mode would auto-allow (the policy engine has the final say)."""
        return self.risk_rank >= risk_rank("HIGH")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "description": self.description,
            "serves": list(self.serves),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "permissions": {"resource": self.permission_resource,
                            "operation": self.permission_operation,
                            "gated": self.gated},
            "risk": self.risk,
            "auth_required": self.auth_required,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "idempotent": self.idempotent,
            "staleness_bound": self.staleness_bound,
            "tags": list(self.tags),
        }


def _allowed_operations(resource: str) -> Tuple[str, ...]:
    from forge.security.policy import RESOURCE_OPERATIONS
    try:
        return tuple(sorted(RESOURCE_OPERATIONS[Resource(resource)]))
    except (ValueError, KeyError):
        return ()


class ToolCapabilityRegistry:
    """Name-indexed tool registry with capability search."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDescriptor] = {}
        self._probes: Dict[str, Callable[[], Dict[str, Any]]] = {}

    # -- registration ----------------------------------------------------------

    def register(self, descriptor: ToolDescriptor, *,
                 availability_probe: Optional[Callable[[], Dict[str, Any]]] = None
                 ) -> None:
        if descriptor.name in self._tools:
            raise ValueError(f"tool already registered: {descriptor.name}")
        self._tools[descriptor.name] = descriptor
        if availability_probe is not None:
            self._probes[descriptor.name] = availability_probe

    def unregister(self, name: str) -> bool:
        self._probes.pop(name, None)
        return self._tools.pop(name, None) is not None

    # -- reads -----------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolDescriptor]:
        return self._tools.get(name)

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._tools))

    def all(self) -> List[ToolDescriptor]:
        return [self._tools[name] for name in self.names()]

    def by_capability(self, capability: str) -> List[ToolDescriptor]:
        return [tool for tool in self.all()
                if tool.capability == capability or capability in tool.serves]

    def availability(self, name: str) -> Dict[str, Any]:
        """Resolve one tool's live status from its probe — honestly.

        No probe => ``architecture`` (the description exists; nothing more is
        claimed). A probe may report state, detail, evidence and a measured
        latency; measured latency overrides the declared estimate *only*
        upward in trust order (evidence beats a static guess).
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"tool": name, "state": "missing",
                    "detail": "not registered", "registered": False}
        probe = self._probes.get(name)
        if probe is None:
            return {"tool": name, "state": "architecture",
                    "detail": "registered; no runtime probe attached",
                    "registered": True, "latency_ms": tool.latency_ms}
        try:
            report = probe() or {}
        except Exception as exc:                       # probe failure is data
            return {"tool": name, "state": "error",
                    "detail": f"probe failed: {type(exc).__name__}",
                    "registered": True, "latency_ms": tool.latency_ms}
        state = str(report.get("state") or "unknown")
        if state not in AVAILABILITY_STATES + ("error", "unknown"):
            state = "unknown"
        measured = report.get("latency_ms")
        latency = float(measured) if isinstance(
            measured, (int, float)) and measured else tool.latency_ms
        return {
            "tool": name, "state": state,
            "detail": str(report.get("detail", ""))[:280],
            "evidence": str(report.get("evidence", ""))[:280],
            "configured": bool(report.get("configured", False)),
            "registered": True,
            "latency_ms": latency,
            "live": state in ("live", "ready"),
        }

    def inventory(self) -> List[Dict[str, Any]]:
        rows = []
        for tool in self.all():
            entry = tool.to_dict()
            entry["availability"] = self.availability(tool.name)
            entry["honesty"] = ("registration is not permission; permission "
                                "is not success")
            rows.append(entry)
        return rows


# -- the built-in surface -------------------------------------------------------

def _descriptor_docs() -> Tuple[ToolDescriptor, ...]:
    """Describe the tools Forge actually has today.

    Every entry maps onto an existing, permissioned path; nothing is listed
    that Forge cannot execute behind policy. Risk labels and schemas describe
    the *worst case* of the path so the planner and the policy gate agree.
    """
    return (
        ToolDescriptor(
            name="web-search", capability="research",
            description="HTTPS-only allowlisted web search (SearXNG/OpenAI) "
                        "through the secure research engine.",
            serves=("research",),
            input_schema={"query": "string"},
            output_schema={"results": "list", "provider": "string"},
            permission_resource=Resource.NETWORK.value,
            permission_operation="request", risk="LOW",
            auth_required=True, staleness_bound=True, idempotent=True,
            tags=("web", "read-only")),
        ToolDescriptor(
            name="deep-research", capability="research",
            description="Multi-source research with citations, cross-source "
                        "comparison and contradiction detection.",
            serves=("research", "documentation"),
            input_schema={"question": "string"},
            output_schema={"report": "object", "citations": "list"},
            permission_resource=Resource.NETWORK.value,
            permission_operation="request", risk="LOW",
            auth_required=True, staleness_bound=True,
            tags=("web", "files", "read-only")),
        ToolDescriptor(
            name="browser", capability="browser",
            description="Policy-gated browser automation "
                        "(forge.tools.browser; mock-safe foundation).",
            serves=("browser",),
            input_schema={"url": "string", "action": "navigate|read|click"},
            output_schema={"content": "string"},
            permission_resource=Resource.BROWSER.value,
            permission_operation="navigate", risk="MEDIUM",
            staleness_bound=True, tags=("web",)),
        ToolDescriptor(
            name="code-execute", capability="coding",
            description="Bounded local Python execution (A48 compute) via "
                        "the permissioned tool runtime.",
            serves=("coding", "testing", "debugging"),
            input_schema={"code": "string"},
            output_schema={"exit_code": "int", "stdout": "string"},
            permission_resource=Resource.TERMINAL.value,
            permission_operation="execute", risk="HIGH",
            idempotent=False, tags=("local", "sandbox-bounded")),
        ToolDescriptor(
            name="repository", capability="coding",
            description="Repository intelligence: symbols, dependencies, "
                        "architecture and test mapping for the project root.",
            serves=("coding", "review", "planning"),
            input_schema={"root": "path"},
            output_schema={"summary": "object"},
            permission_resource=Resource.FILESYSTEM.value,
            permission_operation="read", risk="NONE", idempotent=True,
            tags=("read-only", "local")),
        ToolDescriptor(
            name="files", capability="coding",
            description="Read/write repository files through the ChangeSet "
                        "engine and the policy gate (never direct writes).",
            serves=("coding", "documentation"),
            input_schema={"path": "relative-path", "content": "string"},
            output_schema={"changed": "list"},
            permission_resource=Resource.FILESYSTEM.value,
            permission_operation="write", risk="HIGH",
            tags=("mutating",)),
        ToolDescriptor(
            name="git", capability="coding",
            description="Status/diff/commit through GitTool with explicit "
                        "safe file lists; never `git add .`.",
            serves=("coding",),
            input_schema={"operation": "status|diff|commit|push",
                          "files": "list"},
            output_schema={"result": "string"},
            permission_resource=Resource.GIT.value,
            permission_operation="status", risk="MEDIUM",
            tags=("mutating", "repository")),
        ToolDescriptor(
            name="vision", capability="vision",
            description="Provider-independent image understanding behind "
                        "VISION/analyze approval (A39).",
            serves=("vision",),
            input_schema={"image": "base64"},
            output_schema={"findings": "list", "simulation": "bool"},
            permission_resource=Resource.VISION.value,
            permission_operation="analyze", risk="MEDIUM",
            tags=("untrusted-input",)),
        ToolDescriptor(
            name="voice", capability="text_to_speech",
            description="STT/TTS through the gated voice stack (A36); "
                        "simulated transports are labeled as simulation.",
            serves=("speech_to_text", "text_to_speech"),
            input_schema={"mode": "stt|tts", "payload": "audio|text"},
            output_schema={"text_or_audio": "string"},
            permission_resource=Resource.VOICE.value,
            permission_operation="command", risk="LOW",
            tags=("audio",)),
        ToolDescriptor(
            name="model-inference", capability="reasoning",
            description="External model inference via the Model Fabric "
                        "behind MODEL/call policy (A46).",
            serves=("reasoning", "coding", "planning", "research"),
            input_schema={"prompt": "string", "capability": "string"},
            output_schema={"text": "string", "model": "string"},
            permission_resource=Resource.MODEL.value,
            permission_operation="call", risk="MEDIUM",
            auth_required=True, idempotent=True,
            tags=("fabric",)),
        ToolDescriptor(
            name="memory", capability="reasoning",
            description="Durable memory read/write/delete through the A37 "
                        "gated path (MEMORY resource, single-use tokens).",
            serves=("planning", "reasoning"),
            input_schema={"operation": "read|write|delete", "content": "string"},
            output_schema={"records": "list"},
            permission_resource=Resource.MEMORY.value,
            permission_operation="read", risk="MEDIUM",
            tags=("durable",)),
        ToolDescriptor(
            name="deployment", capability="tool_use",
            description="Validated deployment lifecycle with sha256 "
                        "manifests and one-step rollback (A64).",
            serves=("tool_use",),
            input_schema={"snapshot": "id", "target": "path"},
            output_schema={"status": "string"},
            permission_resource=Resource.TERMINAL.value,
            permission_operation="execute", risk="CRITICAL",
            tags=("mutating", "production")),
        ToolDescriptor(
            name="monitoring", capability="research",
            description="Observability metrics and performance summaries "
                        "(A62/A63), read-only aggregates.",
            serves=("research", "debugging"),
            input_schema={},
            output_schema={"metrics": "object"},
            permission_resource=Resource.FILESYSTEM.value,
            permission_operation="read", risk="NONE", idempotent=True,
            tags=("read-only",)),
    )


def builtin_registry(*, probes: Optional[Dict[str, Callable[[], Dict[str, Any]]]] = None
                     ) -> ToolCapabilityRegistry:
    """Registry seeded with Forge's actual tool surface.

    ``probes`` maps tool name -> zero-arg callable returning an availability
    report; attach real runtime checks (e.g. Ollama health, browser bridge
    state) to move a tool from ``architecture`` to an evidenced state. With
    no probe, a tool honestly reports ``architecture``: registered, not
    claimed live.
    """
    registry = ToolCapabilityRegistry()
    for descriptor in _descriptor_docs():
        probe = (probes or {}).get(descriptor.name)
        registry.register(descriptor, availability_probe=probe)
    return registry
