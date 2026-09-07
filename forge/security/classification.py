"""Data classification and model data policy (A33).

Project content defaults to ``INTERNAL``; obvious sensitive material is
detected and classified upwards. Detection always wins over a caller's
declared level, so secrets cannot be laundered by declaring them public.

Model routing consults :class:`ModelDataPolicy` before sending content to a
provider:

- ``SECRET`` → never to an external model unless explicitly authorized;
- ``CONFIDENTIAL`` → policy-controlled (allow / approval / deny);
- ``INTERNAL`` → configurable for remote models;
- ``PUBLIC`` → normal handling;
- local models always permitted (content never leaves the machine).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from forge.security.policy_gate import PolicyDecision

_SECRET_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}"),
)

_CREDENTIAL_FILENAME_TOKENS = ("credential", "secret", "private_key", "token")


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


_RANK = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.SECRET: 3,
}


def rank(level: DataClassification) -> int:
    return _RANK[level]


def classify_text(text: str, filename: str = "",
                  declared: DataClassification | str | None = None,
                  ) -> DataClassification:
    """Classify text, defaulting to INTERNAL. Detection only raises the level.

    An explicit declaration stands for clean content, but any detected
    signal wins upwards: secrets cannot be laundered by declaring them
    public.
    """
    signal: DataClassification | None = None
    name = PurePosixPath(filename or "").name.lower()
    if name == ".env" or name.endswith(".env"):
        signal = DataClassification.SECRET
    elif any(token in name for token in _CREDENTIAL_FILENAME_TOKENS):
        signal = DataClassification.CONFIDENTIAL
    if any(pattern.search(text or "") for pattern in _SECRET_PATTERNS):
        signal = DataClassification.SECRET
    if declared is None:
        return signal if signal is not None else DataClassification.INTERNAL
    declared_level = (declared if isinstance(declared, DataClassification)
                      else DataClassification(str(declared).lower()))
    if signal is None:
        # Clean content: the explicit declaration stands (even PUBLIC).
        return declared_level
    return signal if rank(signal) >= rank(declared_level) else declared_level


@dataclass
class ModelDataPolicy:
    """Whether classified content may reach a model provider."""

    allow_internal_remote: bool = True
    confidential_remote: str = "approval"  # allow | approval | deny

    def __post_init__(self) -> None:
        if self.confidential_remote not in ("allow", "approval", "deny"):
            raise ValueError(
                f"Unknown confidential_remote mode: {self.confidential_remote!r}")

    def evaluate(self, classification: DataClassification | str, *,
                 local: bool, authorized: bool = False) -> PolicyDecision:
        """Return the data-policy decision for one model call."""
        level = (classification if isinstance(classification, DataClassification)
                 else DataClassification(str(classification).lower()))
        if local:
            return PolicyDecision.ALLOW
        if level == DataClassification.PUBLIC:
            return PolicyDecision.ALLOW
        if level == DataClassification.SECRET:
            return PolicyDecision.ALLOW if authorized else PolicyDecision.DENY
        if level == DataClassification.CONFIDENTIAL:
            return {"allow": PolicyDecision.ALLOW,
                    "approval": PolicyDecision.REQUIRE_APPROVAL,
                    "deny": PolicyDecision.DENY}[self.confidential_remote]
        return (PolicyDecision.ALLOW if self.allow_internal_remote
                else PolicyDecision.REQUIRE_APPROVAL)

    def to_dict(self) -> dict[str, Any]:
        return {"allow_internal_remote": self.allow_internal_remote,
                "confidential_remote": self.confidential_remote}
