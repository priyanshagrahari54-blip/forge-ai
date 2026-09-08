"""General permission policy engine (A33).

This module extends A32's policy architecture — it does not replace it.
:class:`PolicyDecision` (``ALLOW`` / ``DENY`` / ``REQUIRE_APPROVAL``) and the
``SAFE`` / ``ASSISTED`` / ``AUTONOMOUS`` / ``LOCKED`` modes remain
authoritative for the autonomous engineering loop. The engine adds
fine-grained, resource-scoped rules that can only *tighten* an A32 verdict,
never loosen it (see :func:`most_restrictive`); resources with no A32 layer
(browser, desktop, network, model, voice) fail closed by default.

A permission describes WHO (agent), WHAT (operation), WHERE (scope), WHEN
(validity window / timestamp), HOW (request details), RISK, and WHY
(reason). Evaluation is deterministic and side-effect free::

    EXPLICIT DENY
          ↓
    EXPLICIT REQUIRE_APPROVAL
          ↓
    EXPLICIT ALLOW
          ↓
    DEFAULT POLICY (fail closed: DENY)

with one refinement: **the most specific matching rules decide**. Specificity
is a static, documented score (exact scope beats pattern scope; constrained
agent/task/risk/time add weight). All rules tied at the top specificity form
the deciding group, and within that group DENY beats REQUIRE_APPROVAL beats
ALLOW. A broad ALLOW can therefore never override a more specific DENY.

Scope syntax per resource:

- ``filesystem``: exact ``path/to/file``; ``dir/*`` direct children;
  ``dir/**`` or trailing-slash ``dir/`` recursive; ``**`` everything.
- ``browser``: ``example.com`` exact host; ``*.example.com`` subdomains
  (apex not included). Wildcards on bare TLDs are rejected as ambiguous.
- ``network``: host pattern as for browser, plus optional port/protocol.
- ``terminal``: executable scope plus pinned ``args``. An ``ALLOW`` terminal
  rule MUST pin a concrete executable and exact args; broader ALLOWs are
  rejected at validation. ``**`` means any executable and is only valid
  with ``DENY`` / ``REQUIRE_APPROVAL``.
- ``model``: optional provider / model / capability constraints.
- ``git`` / ``desktop`` / ``voice``: operation plus optional target scope.

Rules validate strictly at load time: malformed or ambiguous rules raise
instead of loading partially, and evaluation fails closed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from forge.security.policy_gate import PolicyDecision


class Resource(str, Enum):
    FILESYSTEM = "filesystem"
    TERMINAL = "terminal"
    GIT = "git"
    BROWSER = "browser"
    NETWORK = "network"
    MODEL = "model"
    DESKTOP = "desktop"
    VOICE = "voice"
    MEMORY = "memory"
    AGENT = "agent"


#: Operations each resource understands. Unknown operations never match a
#: rule, so unknown access fails closed.
RESOURCE_OPERATIONS: dict[Resource, frozenset[str]] = {
    Resource.FILESYSTEM: frozenset({
        "read", "write", "create", "modify", "delete", "rename", "execute",
    }),
    Resource.TERMINAL: frozenset({"execute", "read_output"}),
    Resource.GIT: frozenset({"status", "diff", "commit", "push"}),
    Resource.BROWSER: frozenset({
        "navigate", "read", "click", "type", "submit", "upload", "download",
    }),
    Resource.NETWORK: frozenset({"request"}),
    Resource.MODEL: frozenset({"call"}),
    Resource.DESKTOP: frozenset({
        "read_screen", "screenshot", "mouse_move", "mouse_click",
        "keyboard", "launch", "window", "clipboard", "file_access",
        "process", "system_info",
    }),
    Resource.VOICE: frozenset({"command"}),
    Resource.MEMORY: frozenset({"read", "write", "delete"}),
    Resource.AGENT: frozenset({"execute", "message"}),
}

#: A32 tool-permission keys mapped onto engine resources. Used only for the
#: tighten-only integration: the engine may add restrictions to an A32
#: verdict, never remove them.
A32_OPERATION_MAP: dict[str, tuple[Resource, str]] = {
    "read_file": (Resource.FILESYSTEM, "read"),
    "search_files": (Resource.FILESYSTEM, "read"),
    "write_file": (Resource.FILESYSTEM, "write"),
    "delete_file": (Resource.FILESYSTEM, "delete"),
    "run_command": (Resource.TERMINAL, "execute"),
    "run_tests": (Resource.TERMINAL, "execute"),
    "git_status": (Resource.GIT, "status"),
    "git_diff": (Resource.GIT, "diff"),
    "git_commit": (Resource.GIT, "commit"),
    "git_push": (Resource.GIT, "push"),
    "delete_repository": (Resource.FILESYSTEM, "delete"),
    "expose_secrets": (Resource.FILESYSTEM, "read"),
}

_RISK_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_EFFECT_PRECEDENCE = {
    PolicyDecision.DENY: 0,
    PolicyDecision.REQUIRE_APPROVAL: 1,
    PolicyDecision.ALLOW: 2,
}
_NETWORK_PROTOCOLS = frozenset({"tcp", "udp", "http", "https"})


def risk_rank(risk: str) -> int:
    """Rank a risk label; unrecognized labels rank as HIGH (fail closed)."""
    return _RISK_RANK.get((risk or "NONE").upper(), _RISK_RANK["HIGH"])


def most_restrictive(*decisions: PolicyDecision) -> PolicyDecision:
    """Return the strictest of several decisions (DENY wins over everything)."""
    ordered = sorted(decisions, key=lambda item: _EFFECT_PRECEDENCE[item])
    return ordered[0]


def translate_a32(operation: str) -> tuple[Resource, str] | None:
    """Map an A32 tool-permission key onto ``(resource, operation)``."""
    return A32_OPERATION_MAP.get(operation)


def validate_scope(resource: Resource | str, scope: str) -> str:
    """Validate an approval/grant scope, returning its normalized form.

    Raises ``ValueError`` for malformed scopes so approvals can never be
    minted over ambiguous ranges.
    """
    resource = Resource(resource)
    if not isinstance(scope, str) or not scope:
        raise ValueError(f"Scope for {resource.value} must be a non-empty string")
    if resource == Resource.FILESYSTEM:
        return _validate_fs_pattern(scope)
    if resource in (Resource.BROWSER, Resource.NETWORK):
        return _validate_domain_pattern(scope)
    if resource == Resource.TERMINAL and scope == "**":
        raise ValueError("Terminal approvals cannot cover '**'")
    return scope


def scope_for_a32(operation: str, call: dict[str, Any]) -> str:
    """Derive an engine scope from A32 tool-call keyword arguments."""
    if "path" in call and isinstance(call["path"], str):
        return call["path"]
    if operation in ("run_command", "run_tests"):
        command = call.get("command")
        if isinstance(command, list) and command:
            return str(command[0])
        if isinstance(command, str):
            return command
    for key in ("url", "host", "domain"):
        value = call.get(key)
        if isinstance(value, str):
            return value
    return ""


# -- scope helpers -----------------------------------------------------------

def _is_safe_fs_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    return True


def _validate_fs_pattern(pattern: str) -> str:
    """Validate a filesystem scope pattern, returning its normalized form."""
    if pattern in ("", "**"):
        return pattern
    if "\\" in pattern:
        raise ValueError(f"Invalid filesystem scope {pattern!r}: backslashes forbidden")
    candidate = PurePosixPath(pattern)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Invalid filesystem scope {pattern!r}: must be repo-relative")
    normalized = pattern
    if normalized.endswith("/") and normalized != "/":
        normalized = normalized + "**"
    parts = PurePosixPath(normalized).parts
    for index, part in enumerate(parts):
        if "**" in part and part != "**":
            raise ValueError(
                f"Invalid filesystem scope {pattern!r}: '**' must be a full segment")
        if part == "**" and index != len(parts) - 1:
            raise ValueError(
                f"Invalid filesystem scope {pattern!r}: '**' only allowed trailing")
        if "*" in part and part not in ("*", "**"):
            raise ValueError(
                f"Invalid filesystem scope {pattern!r}: '*' must be a full segment")
        if part == "*" and index != len(parts) - 1:
            raise ValueError(
                f"Invalid filesystem scope {pattern!r}: '*' only allowed trailing")
    return normalized


def _match_fs_pattern(pattern: str, scope: str) -> bool:
    if pattern in ("", "**"):
        return _is_safe_fs_path(scope) if pattern == "" else bool(scope) and _is_safe_fs_path(scope)
    if not _is_safe_fs_path(scope):
        return False
    normalized = pattern[:-3] if pattern.endswith("/**") else pattern
    if pattern.endswith("/**"):
        prefix = normalized + "/"
        return scope == normalized or scope.startswith(prefix)
    if pattern.endswith("/*"):
        parent = PurePosixPath(normalized).parent
        candidate = PurePosixPath(scope)
        return candidate.parent == parent and candidate != parent
    return scope == pattern


def _fs_specificity(pattern: str) -> int:
    if pattern in ("", "**"):
        return 0
    depth = len(PurePosixPath(pattern).parts)
    if pattern.endswith("/**"):
        return 10 + depth
    if pattern.endswith("/*"):
        return 50 + depth
    return 100 + depth


def _normalize_host(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    return host


def _validate_domain_pattern(pattern: str) -> str:
    normalized = _normalize_host(pattern)
    if not normalized or " " in normalized or "/" in normalized or "@" in normalized or ":" in normalized:
        raise ValueError(f"Invalid domain scope {pattern!r}")
    labels = normalized.split(".")
    if any(not label for label in labels):
        raise ValueError(f"Invalid domain scope {pattern!r}")
    if labels[0] == "*":
        if len(labels) < 3:
            raise ValueError(
                f"Invalid domain scope {pattern!r}: wildcard needs a registrable domain")
        rest = labels[1:]
    else:
        if "*" in normalized:
            raise ValueError(f"Invalid domain scope {pattern!r}: misplaced wildcard")
        rest = labels
    for label in rest:
        if len(label) > 63 or not all(char.isalnum() or char == "-" for char in label):
            raise ValueError(f"Invalid domain scope {pattern!r}")
    return normalized


def _match_domain_pattern(pattern: str, host: str) -> bool:
    candidate = _normalize_host(host)
    if not candidate or " " in candidate or "/" in candidate:
        return False
    if pattern.startswith("*."):
        apex = pattern[2:]
        return candidate != apex and candidate.endswith("." + apex)
    return candidate == pattern


def _host_of_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return parts.hostname


# -- request / rule ----------------------------------------------------------

@dataclass(frozen=True)
class PermissionRequest:
    """One permission question: WHO/WHAT/WHERE/WHEN/HOW/RISK/WHY."""

    agent: str
    resource: Resource | str
    operation: str
    scope: str = ""
    risk: str = "NONE"
    reason: str = ""
    task_id: str = ""
    trace_id: str = ""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    details: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        resource = Resource(self.resource)  # raises on unknown resource
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "operation", (self.operation or "").lower())
        object.__setattr__(self, "agent", self.agent or "")
        object.__setattr__(self, "risk", (self.risk or "NONE").upper())

    def detail(self, key: str, default: Any = None) -> Any:
        for name, value in self.details:
            if name == key:
                return value
        return default

    def canonical(self) -> tuple[Any, ...]:
        """Hashable identity for the decision cache."""
        return (
            self.agent, self.resource.value, self.operation, self.scope,
            self.risk, self.task_id, tuple(sorted(
                (name, repr(value)) for name, value in self.details)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "agent": self.agent,
            "resource": self.resource.value,
            "operation": self.operation,
            "scope": self.scope,
            "risk": self.risk,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "details": {name: value for name, value in self.details},
        }


@dataclass(frozen=True)
class PermissionRule:
    """One declarative rule: constrained dimensions must ALL match."""

    id: str
    resource: Resource | str
    operation: str
    effect: PolicyDecision | str
    scope: str = ""
    agent: str | None = None
    task_id: str | None = None
    risk_ceiling: str | None = None
    valid_from: float | None = None
    valid_until: float | None = None
    args: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    capability: str | None = None
    port: int | None = None
    protocol: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        resource = Resource(self.resource)
        object.__setattr__(self, "resource", resource)
        if not isinstance(self.operation, str):
            raise ValueError(f"Rule {self.id!r}: operation must be a string")
        operation = (self.operation or "").lower()
        if operation not in RESOURCE_OPERATIONS[resource]:
            raise ValueError(
                f"Rule {self.id!r}: unknown operation {self.operation!r} "
                f"for resource {resource.value!r}")
        object.__setattr__(self, "operation", operation)
        effect = self.effect if isinstance(self.effect, PolicyDecision) else PolicyDecision(str(self.effect).upper())
        object.__setattr__(self, "effect", effect)
        self.validate()
        # Store normalized scopes so matching never depends on spelling.
        if resource == Resource.FILESYSTEM and self.scope:
            object.__setattr__(self, "scope", _validate_fs_pattern(self.scope))
        elif resource in (Resource.BROWSER, Resource.NETWORK) and self.scope:
            object.__setattr__(self, "scope", _validate_domain_pattern(self.scope))

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Rule id must be a non-empty string")
        for field_name in ("scope", "reason"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"Rule {self.id!r}: {field_name} must be a string")
        for field_name in ("agent", "task_id", "risk_ceiling", "provider",
                            "model", "capability", "protocol"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Rule {self.id!r}: {field_name} must be a string")
        for field_name in ("valid_from", "valid_until"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"Rule {self.id!r}: {field_name} must be a number")
        if self.resource == Resource.FILESYSTEM:
            if not self.scope:
                raise ValueError(f"Rule {self.id!r}: filesystem rules need an explicit scope")
            _validate_fs_pattern(self.scope)
        elif self.resource in (Resource.BROWSER, Resource.NETWORK):
            if not self.scope:
                raise ValueError(f"Rule {self.id!r}: {self.resource.value} rules need a domain scope")
            _validate_domain_pattern(self.scope)
        elif self.resource == Resource.TERMINAL:
            if not self.scope:
                raise ValueError(f"Rule {self.id!r}: terminal rules need an executable scope")
            if self.effect == PolicyDecision.ALLOW and (not self.args or self.scope == "**"):
                raise ValueError(
                    f"Rule {self.id!r}: ALLOW terminal rules must pin a concrete "
                    "executable and exact args")
        if self.resource != Resource.TERMINAL and self.args:
            raise ValueError(f"Rule {self.id!r}: args only apply to terminal rules")
        if self.resource != Resource.MODEL and (
                self.provider is not None or self.model is not None
                or self.capability is not None):
            raise ValueError(
                f"Rule {self.id!r}: provider/model/capability only apply to model rules")
        if self.resource != Resource.NETWORK and (
                self.port is not None or self.protocol is not None):
            raise ValueError(
                f"Rule {self.id!r}: port/protocol only apply to network rules")
        if self.port is not None and not (1 <= self.port <= 65535):
            raise ValueError(f"Rule {self.id!r}: port must be within 1-65535")
        if self.protocol is not None and self.protocol not in _NETWORK_PROTOCOLS:
            raise ValueError(f"Rule {self.id!r}: unknown protocol {self.protocol!r}")
        if self.risk_ceiling is not None and self.risk_ceiling.upper() not in _RISK_RANK:
            raise ValueError(f"Rule {self.id!r}: unknown risk ceiling {self.risk_ceiling!r}")
        if (self.valid_from is not None and self.valid_until is not None
                and self.valid_from > self.valid_until):
            raise ValueError(f"Rule {self.id!r}: valid_from is after valid_until")

    @property
    def time_bound(self) -> bool:
        return self.valid_from is not None or self.valid_until is not None

    def specificity(self) -> int:
        """Static match strength: exact beats pattern; constraints add weight."""
        if self.resource == Resource.FILESYSTEM:
            score = _fs_specificity(self.scope)
        elif self.resource in (Resource.BROWSER, Resource.NETWORK):
            score = 10 if self.scope.startswith("*.") else 100
            if self.port is not None:
                score += 20
            if self.protocol is not None:
                score += 5
        elif self.resource == Resource.TERMINAL:
            if self.scope == "**":
                score = 0
            else:
                score = 100 if self.args else 10
        elif self.resource == Resource.MODEL:
            score = 0
            if self.provider is not None:
                score += 20
            if self.model is not None:
                score += 40
            if self.capability is not None:
                score += 10
        elif self.resource == Resource.DESKTOP:
            score = 20 if self.scope else 0
        elif self.resource == Resource.VOICE:
            score = 20 if self.scope else 0
        else:  # git: operation-level, optional path scope
            score = _fs_specificity(self.scope) if self.scope else 0
        if self.agent is not None:
            score += 5
        if self.task_id is not None:
            score += 5
        if self.risk_ceiling is not None:
            score += 2
        if self.time_bound:
            score += 1
        return score

    def matches(self, request: PermissionRequest, now: float | None = None) -> bool:
        if request.resource != self.resource or request.operation != self.operation:
            return False
        if self.agent is not None and request.agent != self.agent:
            return False
        if self.task_id is not None and request.task_id != self.task_id:
            return False
        moment = time.time() if now is None else now
        if self.valid_from is not None and moment < self.valid_from:
            return False
        if self.valid_until is not None and moment >= self.valid_until:
            return False
        if self.risk_ceiling is not None and risk_rank(request.risk) > risk_rank(self.risk_ceiling):
            return False
        return self._scope_matches(request)

    def _scope_matches(self, request: PermissionRequest) -> bool:
        if self.resource == Resource.FILESYSTEM:
            return _match_fs_pattern(self.scope, request.scope)
        if self.resource == Resource.BROWSER:
            host = _host_of_url(request.scope)
            if host is None:
                return False
            return _match_domain_pattern(self.scope, host)
        if self.resource == Resource.NETWORK:
            host = request.detail("host", "")
            if not host or not _match_domain_pattern(self.scope, host):
                return False
            if self.port is not None and request.detail("port") != self.port:
                return False
            if self.protocol is not None and request.detail("protocol") != self.protocol:
                return False
            return True
        if self.resource == Resource.TERMINAL:
            wanted = request.scope
            if self.scope == "**":
                pass  # explicit any-executable scope (never valid with ALLOW)
            elif self.scope != wanted:
                if "/" not in self.scope and "\\" not in self.scope:
                    if wanted.rsplit("/", 1)[-1] != self.scope:
                        return False
                else:
                    return False
            if self.args and tuple(request.detail("args", ())) != tuple(self.args):
                return False
            return True
        if self.resource == Resource.MODEL:
            if self.provider is not None and request.detail("provider") != self.provider:
                return False
            if self.model is not None and request.detail("model") != self.model:
                return False
            if self.capability is not None and request.detail("capability") != self.capability:
                return False
            return True
        if self.resource == Resource.GIT:
            if not self.scope:
                return True
            return _match_fs_pattern(self.scope, request.scope)
        # desktop / voice: exact target scope, empty matches any target.
        if not self.scope:
            return True
        return request.scope == self.scope

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "resource": self.resource.value,
            "operation": self.operation,
            "effect": self.effect.value,
        }
        if self.scope:
            data["scope"] = self.scope
        if self.agent is not None:
            data["agent"] = self.agent
        if self.task_id is not None:
            data["task_id"] = self.task_id
        if self.risk_ceiling is not None:
            data["risk_ceiling"] = self.risk_ceiling
        if self.valid_from is not None:
            data["valid_from"] = self.valid_from
        if self.valid_until is not None:
            data["valid_until"] = self.valid_until
        if self.args:
            data["args"] = list(self.args)
        if self.provider is not None:
            data["provider"] = self.provider
        if self.model is not None:
            data["model"] = self.model
        if self.capability is not None:
            data["capability"] = self.capability
        if self.port is not None:
            data["port"] = self.port
        if self.protocol is not None:
            data["protocol"] = self.protocol
        if self.reason:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class PermissionEvaluation:
    """Result of evaluating one request: decision plus its provenance."""

    decision: PolicyDecision
    matched_rules: tuple[PermissionRule, ...] = ()
    reason: str = ""
    risk: str = "NONE"
    scope: str = ""
    request_id: str = ""
    default_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "matched_rules": [rule.id for rule in self.matched_rules],
            "reason": self.reason,
            "risk": self.risk,
            "scope": self.scope,
            "request_id": self.request_id,
            "default_applied": self.default_applied,
        }


class PermissionPolicy:
    """An indexed, cached, versioned set of permission rules."""

    def __init__(self, rules: tuple[PermissionRule, ...] | list[PermissionRule] = (),
                 default: PolicyDecision = PolicyDecision.DENY) -> None:
        self._rules: list[PermissionRule] = []
        self._index: dict[tuple[Resource, str], list[PermissionRule]] = {}
        self._cache: dict[tuple[Any, ...], PermissionEvaluation] = {}
        self.default = default
        self.version = 0
        for rule in rules:
            self.add_rule(rule)

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return tuple(self._rules)

    def add_rule(self, rule: PermissionRule) -> None:
        rule.validate()
        if any(existing.id == rule.id for existing in self._rules):
            raise ValueError(f"Duplicate rule id: {rule.id!r}")
        self._rules.append(rule)
        self._index.setdefault((rule.resource, rule.operation), []).append(rule)
        self.version += 1
        self._cache.clear()

    def remove_rule(self, rule_id: str) -> bool:
        for index, existing in enumerate(self._rules):
            if existing.id == rule_id:
                del self._rules[index]
                key = (existing.resource, existing.operation)
                self._index[key] = [item for item in self._index[key]
                                    if item.id != rule_id]
                self.version += 1
                self._cache.clear()
                return True
        return False

    def evaluate(self, request: PermissionRequest,
                 now: float | None = None) -> PermissionEvaluation:
        """Evaluate one request. Pure: no side effects, fails closed."""
        moment = time.time() if now is None else now
        candidates = self._index.get((request.resource, request.operation), [])
        cacheable = all(not rule.time_bound for rule in candidates)
        key = (request.canonical(), self.version) if cacheable else None
        if key is not None and key in self._cache:
            return self._cache[key]
        matched = [rule for rule in candidates if rule.matches(request, moment)]
        if not matched:
            evaluation = PermissionEvaluation(
                decision=self.default, reason=(
                    f"No matching rule for {request.resource.value} "
                    f"{request.operation}; default {self.default.value}"),
                risk=request.risk, scope=request.scope,
                request_id=request.request_id, default_applied=True)
        else:
            ordered = sorted(
                matched,
                key=lambda rule: (-rule.specificity(),
                                  _EFFECT_PRECEDENCE[rule.effect], rule.id))
            top = ordered[0].specificity()
            group = tuple(rule for rule in ordered if rule.specificity() == top)
            winner = min(group, key=lambda rule: (
                _EFFECT_PRECEDENCE[rule.effect], rule.id))
            evaluation = PermissionEvaluation(
                decision=winner.effect, matched_rules=group,
                reason=f"Rule {winner.id}: {winner.reason or winner.effect.value} "
                       f"(specificity {top}, {len(group)} deciding rule(s))",
                risk=request.risk, scope=request.scope,
                request_id=request.request_id, default_applied=False)
        if key is not None:
            self._cache[key] = evaluation
        return evaluation

    def simulate(self, request: PermissionRequest,
                 now: float | None = None) -> dict[str, Any]:
        """Dry-run API: decision, matched rules, reason, risk, scope.

        No side effects — evaluation never performs any. Powers tooling such
        as the future browser cockpit's permission preview.
        """
        evaluation = self.evaluate(request, now=now)
        return {
            "decision": evaluation.decision.value,
            "matched_rules": [rule.to_dict() for rule in evaluation.matched_rules],
            "reason": evaluation.reason,
            "risk": evaluation.risk,
            "scope": evaluation.scope,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "default": self.default.value.lower(),
            "rules": [rule.to_dict() for rule in self._rules],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionPolicy":
        """Load a policy from a validated mapping (JSON/YAML compatible).

        Strict: malformed or ambiguous input raises ``ValueError`` and loads
        nothing — never a partial policy.
        """
        errors: list[str] = []
        if not isinstance(data, dict):
            raise ValueError("Policy config must be a mapping")
        for key in data:
            if key not in ("version", "default", "rules"):
                errors.append(f"Unknown policy key: {key!r}")
        version = data.get("version", 1)
        if version != 1:
            errors.append(f"Unsupported policy version: {version!r}")
        default_raw = data.get("default", "deny")
        try:
            default = PolicyDecision(str(default_raw).upper())
        except ValueError:
            errors.append(f"Unknown default decision: {default_raw!r}")
            default = PolicyDecision.DENY
        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            errors.append("Policy 'rules' must be a list")
            raw_rules = []
        rules: list[PermissionRule] = []
        seen: set[str] = set()
        allowed_keys = {"id", "resource", "operation", "effect", "scope",
                        "agent", "task_id", "risk_ceiling", "valid_from",
                        "valid_until", "args", "provider", "model",
                        "capability", "port", "protocol", "reason"}
        for index, raw in enumerate(raw_rules):
            label = f"rules[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{label} must be a mapping")
                continue
            for key in raw:
                if key not in allowed_keys:
                    errors.append(f"{label}: unknown key {key!r}")
            for required in ("id", "resource", "operation", "effect"):
                if required not in raw:
                    errors.append(f"{label}: missing required key {required!r}")
            rule_id = raw.get("id")
            if isinstance(rule_id, str) and rule_id in seen:
                errors.append(f"{label}: duplicate rule id {rule_id!r}")
            if isinstance(rule_id, str):
                seen.add(rule_id)
            args = raw.get("args", ())
            if not isinstance(args, (list, tuple)) or not all(
                    isinstance(item, str) for item in args):
                errors.append(f"{label}: 'args' must be a list of strings")
                args = ()
            port = raw.get("port")
            if port is not None and (not isinstance(port, int)
                                     or isinstance(port, bool)
                                     or not (1 <= port <= 65535)):
                errors.append(f"{label}: 'port' must be an int within 1-65535")
                port = None
            for numeric in ("valid_from", "valid_until"):
                value = raw.get(numeric)
                if value is not None and not isinstance(value, (int, float)):
                    errors.append(f"{label}: {numeric!r} must be a number")
            if errors:
                continue
            try:
                rules.append(PermissionRule(
                    id=raw.get("id", ""), resource=raw.get("resource", ""),
                    operation=raw.get("operation", ""),
                    effect=str(raw.get("effect", "")).upper(),
                    scope=raw.get("scope", ""), agent=raw.get("agent"),
                    task_id=raw.get("task_id"),
                    risk_ceiling=raw.get("risk_ceiling"),
                    valid_from=raw.get("valid_from"),
                    valid_until=raw.get("valid_until"),
                    args=tuple(args), provider=raw.get("provider"),
                    model=raw.get("model"), capability=raw.get("capability"),
                    port=port, protocol=raw.get("protocol"),
                    reason=raw.get("reason", "")))
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
        if errors:
            raise ValueError("Invalid policy config: " + "; ".join(errors))
        return cls(rules=rules, default=default)


# -- profiles -----------------------------------------------------------------

def _base_read_rules(prefix: str) -> list[PermissionRule]:
    return [
        PermissionRule(f"{prefix}-fs-read", Resource.FILESYSTEM, "read",
                         PolicyDecision.ALLOW, scope="**",
                         reason="Read-only inspection is always permitted"),
        PermissionRule(f"{prefix}-git-status", Resource.GIT, "status",
                         PolicyDecision.ALLOW,
                         reason="Repository status inspection is read-only"),
        PermissionRule(f"{prefix}-git-diff", Resource.GIT, "diff",
                         PolicyDecision.ALLOW,
                         reason="Repository diff inspection is read-only"),
    ]


def locked_profile() -> PermissionPolicy:
    """No modifications; reads only. New resources default-deny."""
    return PermissionPolicy(rules=_base_read_rules("locked"))


def safe_profile(allowed_sites: tuple[str, ...] = (),
                 allowed_hosts: tuple[str, ...] = ()) -> PermissionPolicy:
    """Read/analyze only, plus optional read-only site/host lists."""
    rules = _base_read_rules("safe")
    for index, site in enumerate(allowed_sites):
        rules.append(PermissionRule(
            f"safe-site-read-{index}", Resource.BROWSER, "read",
            PolicyDecision.ALLOW, scope=site,
            reason="Explicitly listed readable site"))
    for index, host in enumerate(allowed_hosts):
        rules.append(PermissionRule(
            f"safe-host-{index}", Resource.NETWORK, "request",
            PolicyDecision.ALLOW, scope=host, protocol="https", port=443,
            reason="Explicitly listed HTTPS host"))
    return PermissionPolicy(rules=rules)


def _browser_write_rules(prefix: str, effect: PolicyDecision,
                         sites: tuple[str, ...]) -> list[PermissionRule]:
    rules = []
    for index, site in enumerate(sites):
        for offset, operation in enumerate(("navigate", "read")):
            rules.append(PermissionRule(
                f"{prefix}-site-{operation}-{index}", Resource.BROWSER,
                operation, PolicyDecision.ALLOW, scope=site,
                reason="Explicitly listed site"))
        for offset, operation in enumerate(("submit", "upload")):
            rules.append(PermissionRule(
                f"{prefix}-site-{operation}-{index}", Resource.BROWSER,
                operation, effect, scope=site,
                reason="Site mutation needs explicit authorization"))
    return rules


def assisted_profile(allowed_sites: tuple[str, ...] = (),
                     allowed_hosts: tuple[str, ...] = ()) -> PermissionPolicy:
    """Reads flow; every mutation needs explicit approval."""
    rules = _base_read_rules("assisted")
    for operation in ("write", "create", "modify", "delete", "rename", "execute"):
        rules.append(PermissionRule(
            f"assisted-fs-{operation}", Resource.FILESYSTEM, operation,
            PolicyDecision.REQUIRE_APPROVAL, scope="**",
            reason="Assisted mode: writes require approval"))
    rules.append(PermissionRule(
        "assisted-terminal", Resource.TERMINAL, "execute",
        PolicyDecision.REQUIRE_APPROVAL, scope="**",
        reason="Assisted mode: terminal execution requires approval"))
    for operation in ("commit", "push"):
        rules.append(PermissionRule(
            f"assisted-git-{operation}", Resource.GIT, operation,
            PolicyDecision.REQUIRE_APPROVAL,
            reason="Assisted mode: publishing requires approval"))
    rules.extend(_browser_write_rules(
        "assisted", PolicyDecision.REQUIRE_APPROVAL, allowed_sites))
    for index, host in enumerate(allowed_hosts):
        rules.append(PermissionRule(
            f"assisted-host-{index}", Resource.NETWORK, "request",
            PolicyDecision.REQUIRE_APPROVAL, scope=host,
            reason="Assisted mode: network use requires approval"))
    return PermissionPolicy(rules=rules)


def autonomous_profile(allowed_sites: tuple[str, ...] = (),
                       allowed_hosts: tuple[str, ...] = ()) -> PermissionPolicy:
    """Low-risk writes flow; destructive/sensitive/mutating acts need approval."""
    rules = _base_read_rules("autonomous")
    for operation in ("write", "create", "modify"):
        rules.append(PermissionRule(
            f"autonomous-fs-{operation}", Resource.FILESYSTEM, operation,
            PolicyDecision.ALLOW, scope="**", risk_ceiling="MEDIUM",
            reason="Autonomous mode: low-risk writes auto-approved"))
        # Lower-specificity catch-all: writes above the ceiling require
        # approval instead of falling through to the default deny.
        rules.append(PermissionRule(
            f"autonomous-fs-{operation}-high", Resource.FILESYSTEM, operation,
            PolicyDecision.REQUIRE_APPROVAL, scope="**",
            reason="Autonomous mode: high-risk writes require approval"))
    for operation in ("delete", "rename", "execute"):
        rules.append(PermissionRule(
            f"autonomous-fs-{operation}", Resource.FILESYSTEM, operation,
            PolicyDecision.REQUIRE_APPROVAL, scope="**",
            reason="Autonomous mode: destructive writes require approval"))
    rules.append(PermissionRule(
        "autonomous-terminal", Resource.TERMINAL, "execute",
        PolicyDecision.REQUIRE_APPROVAL, scope="**",
        reason="Autonomous mode: terminal execution requires approval"))
    for operation in ("commit", "push"):
        rules.append(PermissionRule(
            f"autonomous-git-{operation}", Resource.GIT, operation,
            PolicyDecision.REQUIRE_APPROVAL,
            reason="Autonomous mode: publishing requires approval"))
    rules.extend(_browser_write_rules(
        "autonomous", PolicyDecision.REQUIRE_APPROVAL, allowed_sites))
    for index, host in enumerate(allowed_hosts):
        rules.append(PermissionRule(
            f"autonomous-host-{index}", Resource.NETWORK, "request",
            PolicyDecision.ALLOW, scope=host, protocol="https", port=443,
            reason="Explicitly listed HTTPS host"))
    return PermissionPolicy(rules=rules)


def custom_profile(rules: list[PermissionRule],
                   default: PolicyDecision = PolicyDecision.DENY) -> PermissionPolicy:
    """User-defined rule set. Cannot override an explicit DENY: precedence is
    structural (see :func:`most_restrictive` and the evaluation order)."""
    return PermissionPolicy(rules=list(rules), default=default)
