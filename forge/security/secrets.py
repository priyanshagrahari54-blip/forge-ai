from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Pattern


class SecretType(str, Enum):
    """Categories of secrets Forge can detect and redact."""

    AWS_ACCESS_KEY = "aws_access_key"
    GITHUB_TOKEN = "github_token"
    SLACK_TOKEN = "slack_token"
    STRIPE_KEY = "stripe_key"
    JWT = "jwt"
    PRIVATE_KEY = "private_key"
    API_KEY = "api_key"
    CONNECTION_STRING = "connection_string"
    PASSWORD = "password"


REDACTED_MARKER = "<redacted>"


@dataclass(frozen=True)
class SecretPattern:
    """A compiled secret-detection pattern.

    ``value_group`` identifies the capture group that holds the actual
    secret value when only part of the match should be replaced (for
    ``key=value`` style assignments). When ``None`` the whole match is
    considered the secret.
    """

    type: SecretType
    regex: Pattern[str]
    confidence: float
    value_group: int | None = None


@dataclass(frozen=True)
class SecretFinding:
    """A detected secret and where it was found.

    ``snippet`` is always redacted; it never contains the secret value.
    """

    type: SecretType
    line: int
    column: int
    snippet: str
    confidence: float


class SecretScanner:
    """Detect common secret formats without leaking their values."""

    PATTERNS: tuple[SecretPattern, ...] = (
        SecretPattern(
            SecretType.AWS_ACCESS_KEY,
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            0.99,
        ),
        SecretPattern(
            SecretType.GITHUB_TOKEN,
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
            0.99,
        ),
        SecretPattern(
            SecretType.SLACK_TOKEN,
            re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
            0.99,
        ),
        SecretPattern(
            SecretType.STRIPE_KEY,
            re.compile(r"\b(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{16,}\b"),
            0.99,
        ),
        SecretPattern(
            SecretType.JWT,
            re.compile(
                r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
                r"[A-Za-z0-9_-]{10,}\b"
            ),
            0.98,
        ),
        SecretPattern(
            SecretType.PRIVATE_KEY,
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            0.99,
        ),
        SecretPattern(
            SecretType.API_KEY,
            re.compile(
                r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|"
                r"access[_-]?token|auth[_-]?token|client[_-]?secret)\b"
                r"\s*[=:]\s*['\"]?([A-Za-z0-9_\-\./+\$]{8,})['\"]?"
            ),
            0.80,
            value_group=1,
        ),
        SecretPattern(
            SecretType.PASSWORD,
            re.compile(
                r"(?i)\bpassword\b\s*[=:]\s*['\"]?([^\s'\"]{6,})['\"]?"
            ),
            0.75,
            value_group=1,
        ),
        SecretPattern(
            SecretType.CONNECTION_STRING,
            re.compile(
                r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
                r"://[^\s'\"]*:[^\s'\"]+@"
            ),
            0.95,
        ),
    )

    def _match_spans(
        self,
        text: str,
    ) -> list[tuple[int, int, int, SecretType, float]]:
        """Return (line, start, end, type, confidence) for each match.

        Matches are deterministic: ordered by line then column, with
        overlapping lower-confidence matches dropped.
        """
        spans: list[tuple[int, int, int, SecretType, float]] = []
        lines = text.splitlines()

        for line_number, line in enumerate(lines, start=1):
            line_spans: list[tuple[int, int, SecretType, float]] = []

            for pattern in self.PATTERNS:
                for match in pattern.regex.finditer(line):
                    start, end = match.span(pattern.value_group or 0)
                    line_spans.append(
                        (
                            start,
                            end,
                            pattern.type,
                            pattern.confidence,
                        )
                    )

            line_spans.sort(
                key=lambda span: (
                    span[0],
                    -span[3],
                    span[2].value,
                )
            )

            for start, end, secret_type, confidence in line_spans:
                if spans and (
                    spans[-1][0] == line_number
                    and start < spans[-1][2]
                ):
                    continue

                spans.append(
                    (
                        line_number,
                        start,
                        end,
                        secret_type,
                        confidence,
                    )
                )

        return spans

    def scan(self, text: str) -> list[SecretFinding]:
        """Return all detected secrets with redacted snippets."""
        findings: list[SecretFinding] = []
        lines = text.splitlines()

        for line, start, end, secret_type, confidence in self._match_spans(
            text
        ):
            line_text = lines[line - 1]
            left = line_text[max(0, start - 16):start]
            right = line_text[end : end + 16]

            findings.append(
                SecretFinding(
                    type=secret_type,
                    line=line,
                    column=start + 1,
                    snippet=f"{left}{REDACTED_MARKER}{right}",
                    confidence=confidence,
                )
            )

        return findings


class SecretRedactor:
    """Replace detected secrets with a fixed marker.

    The redacted output never contains a detected secret value, which
    guarantees that tool responses and audit messages cannot leak
    credentials through Forge.
    """

    def __init__(
        self,
        scanner: SecretScanner | None = None,
    ) -> None:
        self.scanner = scanner or SecretScanner()

    def redact(self, text: str) -> tuple[str, list[SecretFinding]]:
        """Return ``(redacted_text, findings)`` for the given text."""
        rebuilt_lines: list[str] = []
        findings: list[SecretFinding] = []

        for line_number, line in enumerate(
            text.splitlines(keepends=True),
            start=1,
        ):
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            matches = self._line_matches(body)

            if not matches:
                rebuilt_lines.append(line)
                continue

            rebuilt: list[str] = []
            cursor = 0

            for start, end, secret_type, confidence in matches:
                rebuilt.append(body[cursor:start])
                rebuilt.append(REDACTED_MARKER)
                cursor = end

                left = body[max(0, start - 16):start]
                right = body[end : end + 16]

                findings.append(
                    SecretFinding(
                        type=secret_type,
                        line=line_number,
                        column=start + 1,
                        snippet=f"{left}{REDACTED_MARKER}{right}",
                        confidence=confidence,
                    )
                )

            rebuilt.append(body[cursor:])
            rebuilt_lines.append("".join(rebuilt) + newline)

        return "".join(rebuilt_lines), findings

    def _line_matches(
        self,
        line: str,
    ) -> list[tuple[int, int, SecretType, float]]:
        """Return deterministic, non-overlapping matches for one line."""
        candidates: list[tuple[int, int, SecretType, float]] = []

        for pattern in self.scanner.PATTERNS:
            for match in pattern.regex.finditer(line):
                start, end = match.span(pattern.value_group or 0)
                candidates.append(
                    (
                        start,
                        end,
                        pattern.type,
                        pattern.confidence,
                    )
                )

        candidates.sort(
            key=lambda span: (
                span[0],
                -span[3],
                span[2].value,
            )
        )

        selected: list[tuple[int, int, SecretType, float]] = []

        for start, end, secret_type, confidence in candidates:
            if selected and start < selected[-1][1]:
                continue

            selected.append(
                (
                    start,
                    end,
                    secret_type,
                    confidence,
                )
            )

        return selected