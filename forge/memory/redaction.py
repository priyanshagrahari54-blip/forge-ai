"""Secret detection and redaction for long-term memory.

Forge memory must *never* store API keys, passwords, tokens, private keys,
or other secrets. This module scans every candidate memory item before it
is persisted and either redacts secret-looking spans in place or refuses
the item outright when the content *is* a secret.

The scanner is deterministic and conservative: it reuses the repository's
central ``redact_text`` vocabulary and layers a wider set of well-known
secret shapes on top. Detection is always done *before* persistence, so
the database only ever contains the redacted text and a redaction count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from forge.core.report import redact_text

REDACTED = "***REDACTED***"

#: Secret-looking spans we additionally recognize (beyond core report redaction).
_SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    # GitHub / GitLab / common hosted-provider tokens.
    ("api_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("api_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("api_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    # OpenAI / Anthropic style keys.
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    # Google API key (AIza...) and generic alnum keys.
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    # Slack tokens (xoxb- / xoxp-).
    ("api_token", re.compile(r"\bxox[bpasr]-[0-9A-Za-z\-]{10,}\b")),
    # Stripe live/test keys.
    ("api_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}\b")),
    # JWT: three base64url segments separated by dots.
    ("token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    # AWS access key id / secret key.
    ("api_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # Generic assignment-style secrets: key = value with a quoted value.
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|pwd|token|"
            r"access[_-]?token|client[_-]?secret|private[_-]?key|"
            r"auth[_-]?token)\b\s*[:=]\s*[\"']?[^\"'\s,;]{4,}[\"']?"
        ),
    ),
    # Bearer authorization headers.
    ("token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{12,}", re.I)),
    # PEM private keys.
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.+?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.S,
        ),
    ),
    # Database connection strings with credentials.
    (
        "credentials",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
            r"[^\s\"']+:[^\s\"']+@"
        ),
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int
    snippet: str  # never the secret itself; a safe label

    def to_dict(self):
        return {"kind": self.kind, "start": self.start, "end": self.end,
                "snippet": self.snippet}


@dataclass(frozen=True)
class SecretScan:
    """Result of scanning one candidate content string."""

    clean: bool
    findings: tuple
    redacted_text: str
    redaction_count: int

    @property
    def is_entirely_secret(self) -> bool:
        """True when the content is *nothing but* a secret (refuse it)."""
        if not self.findings:
            return False
        redacted_cover = self.redacted_text.strip()
        # If, after redaction, essentially no readable text remains, the
        # whole string was secret material.
        leftover = redacted_cover.replace(REDACTED, "")
        stripped = "".join(ch for ch in leftover if ch.isalnum())
        return len(stripped) < 4


def _redact_all(text: str) -> Tuple[str, int, tuple]:
    """Apply the extended vocabulary and return (text, count, findings)."""
    findings: List[SecretFinding] = []
    working = text
    for kind, pattern in _SECRET_PATTERNS:
        matches = list(pattern.finditer(working))
        for match in matches:
            findings.append(SecretFinding(
                kind=kind, start=match.start(), end=match.end(),
                snippet=f"<{kind} redacted>"))
        working = pattern.sub(REDACTED, working)
    # Central redaction catches the base vocabulary (keys, AWS ids, db urls,
    # bearer tokens, private-key blocks) and is idempotent.
    working = redact_text(working)
    count = working.count(REDACTED)
    return working, count, tuple(findings)


def scan_secrets(content: str) -> SecretScan:
    """Scan and redact one string; returns a :class:`SecretScan`."""
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    redacted, count, findings = _redact_all(content)
    return SecretScan(
        clean=count == 0,
        findings=findings,
        redacted_text=redacted,
        redaction_count=count,
    )


def contains_secret(content: str) -> bool:
    """True if the content contains any secret-looking span."""
    return not scan_secrets(content).clean


def redact(content: str) -> str:
    """Convenience: return the redacted text only."""
    return scan_secrets(content).redacted_text
