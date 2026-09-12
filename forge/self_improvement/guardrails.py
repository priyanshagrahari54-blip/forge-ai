"""Hard guardrails for self-improvement (A81).

Forge must never, through its own improvement loop:

- remove its own security controls,
- increase its own permissions,
- disable policy,
- bypass approvals,
- modify credentials,
- modify protected files without explicit authorization.

The guardrails are evaluated *before* any candidate is applied — on the
proposal (declared affected files) and again on the concrete change set
(paths and content). They cannot be relaxed by configuration: the protected
path list and the content rules are module constants, and the guardrail
module itself is on the protected list.

Explicit authorization for a protected file is an operator-supplied
:class:`ProtectedFileAuthorization` naming the exact paths. The loop can
never mint one, and even with one the content rules still apply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

#: Paths (posix, repository-relative) or path prefixes that are protected.
#: Prefixes end with ``/``. Matching is exact-or-prefix on normalized paths.
PROTECTED_PATHS: tuple[str, ...] = (
    ".git/",
    ".forge/",
    ".github/",
    ".env",
    "pyproject.toml",
    "forge/security/",
    "forge/control/approvals.py",
    "forge/control/sessions.py",
    "forge/api/deps.py",
    "forge/tools/change_applier.py",
    "forge/tools/checkpoint.py",
    "forge/core/acceptance.py",
    "forge/final/",
    "forge/autonomy/",
    "forge/models/credentials.py",
    "forge/self_improvement/guardrails.py",
    "forge/self_improvement/acceptance.py",
    "forge/self_improvement/ledger.py",
    "forge/self_improvement/rollback.py",
)

#: Filename fragments that mark credential material (always refused, even
#: with a protected-file authorization).
CREDENTIAL_NAME_TOKENS: tuple[str, ...] = (
    "credential", "secret", "id_rsa", "id_dsa", "id_ed25519", "id_ecdsa",
    ".pem", ".key", ".p12", ".pfx", "token", "password", ".netrc",
    "api_key", "apikey",
)

#: Content patterns that indicate the change weakens security, permissions,
#: policy, or approvals. Each is ``(rule, compiled regex)``.
FORBIDDEN_CONTENT_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("permission escalation",
     re.compile(r"PermissionLevel\.(?:BLOCKED|APPROVAL_REQUIRED)\s*(?:->|to|=)\s*PermissionLevel\.SAFE")),
    ("permission escalation",
     re.compile(r"[\"'](?:delete_file|delete_repository|run_command|git_commit|git_push|expose_secrets)[\"']\s*:\s*PermissionLevel\.SAFE")),
    ("policy disabled",
     re.compile(r"^\s*(?:self\.)?(?:policy|policy_gate|gate|guardrails?|approvals?|manager)\s*=\s*None\s*$", re.M)),
    ("policy disabled",
     re.compile(r"\b(?:disable|skip|bypass)_(?:policy|gate|guardrails?|approvals?|security|verification)\b")),
    ("approval bypass",
     re.compile(r"\bapproved\s*=\s*True\b")),
    ("approval bypass",
     re.compile(r"\bpolicy_approval\s*=\s*(?:True|PolicyApproval\()")),
    ("approval bypass",
     re.compile(r"\brequire_(?:approval|policy|policy_approval|tests|security)\s*=\s*False\b")),
    ("approval bypass",
     re.compile(r"\bauto_approve\s*=\s*True\b")),
    ("mode escalation",
     re.compile(r"OperationMode\.(?:SAFE|ASSISTED|LOCKED)\s*(?:->|to)\s*OperationMode\.AUTONOMOUS")),
    ("mode escalation",
     re.compile(r"\bmode\s*=\s*(?:OperationMode\.AUTONOMOUS|[\"']autonomous[\"'])")),
    ("credential material",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("credential material",
     re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I)),
    ("credential material",
     re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("dangerous execution",
     re.compile(r"\b(?:eval|exec)\s*\(|\bos\.system\s*\(|\bos\.popen\s*\(|shell\s*=\s*True")),
    ("dangerous execution",
     re.compile(r"^\s*(?:import|from)\s+(?:ctypes|marshal)\b", re.M)),
    ("network egress",
     re.compile(r"^\s*(?:import|from)\s+(?:socket|urllib\.request|http\.client|requests|httpx)\b", re.M)),
    ("test weakening",
     re.compile(r"\bpytest\.(?:skip|xfail)\s*\(|@pytest\.mark\.(?:skip|skipif|xfail)\b")),
    ("test weakening",
     re.compile(r"^\s*assert\s+(?:True|1)\s*(?:#.*)?$", re.M)),
    ("test weakening",
     re.compile(r"^\s*(?:sys\.exit|os\._exit)\s*\(\s*0\s*\)", re.M)),
    ("environment tampering",
     re.compile(r"os\.environ\s*\[\s*[\"'](?:FORGE_|OPENAI_|OLLAMA_)")),
)

#: Identifiers whose *removal* from a file is treated as removing a security
#: control (checked when both the original and new content are available).
SECURITY_CONTROL_TOKENS: tuple[str, ...] = (
    "PolicyGate", "PermissionManager", "Guardrails", "AcceptanceGate",
    "VerificationPipeline", "require_policy_approval", "_is_protected_path",
    "PROTECTED_PATHS", "FORBIDDEN_CONTENT_PATTERNS", "approval_token",
    "ApprovalStore", "check_guardrails", "PolicyApproval",
)


class GuardrailViolation(Exception):
    """Raised when a proposal or change set violates a hard guardrail."""

    def __init__(self, violations: list[dict[str, str]]) -> None:
        self.violations = list(violations)
        summary = "; ".join(
            f"{item['rule']}: {item.get('path', '') or item.get('detail', '')}"
            for item in self.violations[:5])
        super().__init__(f"guardrail violation — {summary}")


@dataclass(frozen=True)
class ProtectedFileAuthorization:
    """Operator-issued, path-exact authorization to touch protected files.

    ``authorized_by`` must be a human actor identifier; the self-improvement
    loop never constructs one. Credential files stay refused regardless.
    """

    paths: tuple[str, ...]
    authorized_by: str
    reason: str = ""
    ticket: str = ""

    def covers(self, path: str) -> bool:
        normalized = normalize_path(path)
        return any(normalize_path(p) == normalized for p in self.paths)


def normalize_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_protected_path(path: str) -> bool:
    normalized = normalize_path(path)
    if not normalized:
        return True
    pure = PurePosixPath(normalized)
    if ".." in pure.parts or pure.is_absolute():
        return True
    for entry in PROTECTED_PATHS:
        if entry.endswith("/"):
            if normalized.startswith(entry) or normalized == entry.rstrip("/"):
                return True
        elif normalized == entry:
            return True
    return False


def is_credential_path(path: str) -> bool:
    name = PurePosixPath(normalize_path(path)).name.lower()
    if not name:
        return False
    if name == ".env" or name.endswith(".env"):
        return True
    return any(token in name for token in CREDENTIAL_NAME_TOKENS)


@dataclass
class Guardrails:
    """Stateless evaluator; the constants above are the only policy."""

    authorization: ProtectedFileAuthorization | None = None
    extra_violations: list[dict[str, str]] = field(default_factory=list)

    # -- path checks ----------------------------------------------------------

    def check_paths(self, paths: Iterable[str]) -> list[dict[str, str]]:
        violations: list[dict[str, str]] = []
        for raw in paths:
            path = normalize_path(raw)
            if is_credential_path(path):
                violations.append({"rule": "credential file", "path": path})
                continue
            if is_protected_path(path):
                if self.authorization is not None and self.authorization.covers(path) \
                        and self.authorization.authorized_by.strip():
                    continue
                violations.append({"rule": "protected file without authorization",
                                   "path": path})
        return violations

    # -- content checks -------------------------------------------------------

    def check_content(self, path: str, content: str,
                      original: str | None = None) -> list[dict[str, str]]:
        violations: list[dict[str, str]] = []
        text = content or ""
        for rule, pattern in FORBIDDEN_CONTENT_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            # A rule that already existed verbatim in the original is not a
            # *new* weakening; the loop still cannot introduce one.
            if original is not None and pattern.search(original):
                continue
            violations.append({"rule": rule, "path": normalize_path(path),
                               "detail": match.group(0)[:80]})
        if original:
            for token in SECURITY_CONTROL_TOKENS:
                pattern = re.compile(r"\b" + re.escape(token) + r"\b")
                before = len(pattern.findall(original))
                after = len(pattern.findall(text))
                if before and after < before:
                    violations.append({
                        "rule": "security control removed",
                        "path": normalize_path(path),
                        "detail": f"{token} ({before} -> {after} references)"})
            # Deleting assertions from a test file weakens verification.
            is_test = "/tests/" in "/" + normalize_path(path) or normalize_path(path).split("/")[-1].startswith("test_")
            if is_test:
                before_asserts = len(re.findall(r"^\s*assert\b", original, re.M))
                after_asserts = len(re.findall(r"^\s*assert\b", text, re.M))
                if after_asserts < before_asserts:
                    violations.append({"rule": "test weakening", "path": normalize_path(path),
                                       "detail": f"assertions {before_asserts} -> {after_asserts}"})
        return violations

    # -- combined -------------------------------------------------------------

    def check_changes(self, changes: dict[str, str],
                      originals: dict[str, str | None] | None = None
                      ) -> list[dict[str, str]]:
        """Evaluate a ``{path: new_content}`` change set."""
        violations = self.check_paths(changes.keys())
        for path, content in changes.items():
            original = (originals or {}).get(path)
            violations.extend(self.check_content(path, content, original))
        return violations + list(self.extra_violations)

    def enforce_paths(self, paths: Iterable[str]) -> None:
        violations = self.check_paths(paths)
        if violations:
            raise GuardrailViolation(violations)

    def enforce_changes(self, changes: dict[str, str],
                        originals: dict[str, str | None] | None = None) -> None:
        violations = self.check_changes(changes, originals)
        if violations:
            raise GuardrailViolation(violations)

    @staticmethod
    def describe() -> dict[str, Any]:
        return {
            "protected_paths": list(PROTECTED_PATHS),
            "credential_name_tokens": list(CREDENTIAL_NAME_TOKENS),
            "forbidden_content_rules": sorted({rule for rule, _ in FORBIDDEN_CONTENT_PATTERNS}
                                              | {"security control removed", "undeclared write"}),
            "security_control_tokens": list(SECURITY_CONTROL_TOKENS),
            "invariants": [
                "never remove its own security controls",
                "never increase its own permissions",
                "never disable policy",
                "never bypass approvals",
                "never modify credentials",
                "never modify protected files without explicit authorization",
            ],
        }
