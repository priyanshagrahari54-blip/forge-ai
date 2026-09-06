"""Secure credential handling for optional remote providers.

Credentials are resolved from environment variables by default. An operator may
additionally point Forge at a user-owned JSON secrets file; Forge refuses to
read one that is group/world accessible. Secret *values* are never exposed in
``repr``, logs, telemetry, or configuration dumps.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping


class CredentialError(RuntimeError):
    """Raised when a required credential is missing or unreadable."""


#: Environment variable names used to discover provider credentials. Providers
#: that require no credential (local, Ollama) are intentionally absent.
ENV_VAR_NAMES: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
}

#: Providers that are always credential-free.
UNCREDENTIALED_PROVIDERS: tuple[str, ...] = ("local", "ollama", "mock")


class CredentialStore:
    """Resolve provider credentials without ever leaking their values."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        files: tuple[str | Path, ...] = (),
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        self._files: dict[str, str] = {}
        for path in files:
            self._files.update(self._load_file(Path(path)))

    @staticmethod
    def _load_file(path: Path) -> dict[str, str]:
        path = path.resolve()
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as exc:
            raise CredentialError(f"Credential file not readable: {path}") from exc
        # Refuse to read secret material readable by group/other. This protects
        # against accidentally world-readable credential files.
        if mode & 0o077:
            raise CredentialError(
                f"Credential file has unsafe permissions ({mode:o}); "
                f"expected 600: {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Credential file is not valid JSON: {path}") from exc
        if not isinstance(data, dict):
            raise CredentialError(f"Credential file must contain a JSON object: {path}")
        return {str(key): str(value) for key, value in data.items() if value is not None}

    def get(self, provider: str, default: str | None = None) -> str | None:
        """Return the credential for *provider*, or *default* if absent."""
        provider = provider.lower()
        if provider in self._files:
            return self._files[provider]
        for env_name in ENV_VAR_NAMES.get(provider, ()):
            if env_name in self._env and self._env[env_name]:
                return self._env[env_name]
        return default

    def require(self, provider: str) -> str:
        """Return the credential or raise ``CredentialError``."""
        value = self.get(provider)
        if not value:
            raise CredentialError(f"No credential configured for provider {provider!r}")
        return value

    def configured(self, provider: str) -> bool:
        """True when a credential is present. Never reveals the value."""
        return self.get(provider) is not None

    def providers(self) -> dict[str, bool]:
        """Provider name -> configured boolean, with values redacted."""
        names: set[str] = set(self._files)
        for provider in ENV_VAR_NAMES:
            names.add(provider)
        return {provider: self.configured(provider) for provider in sorted(names)}

    def __repr__(self) -> str:
        configured = [name for name, present in self.providers().items() if present]
        return f"<CredentialStore configured=[{', '.join(configured)}]>"
