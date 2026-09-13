"""Forge Desktop client connection settings (A81).

One small JSON file holds everything *except* the secret:

.. code-block:: json

    {
      "version": 1,
      "server_url": "http://192.168.1.20:8000",
      "client_id": "g560",
      "project_id": "demo",
      "mode": "hybrid",
      "request_timeout": 15.0,
      "heartbeat_seconds": 10.0,
      "reconnect": {"initial_delay": 1.0, "max_delay": 30.0,
                     "multiplier": 2.0, "max_attempts": 0},
      "local": {"allow_local": true, "allow_server": true,
                 "max_task_chars": 2000, "min_free_ram_mb": 512}
    }

The shared secret lives in a separate file (``<config_dir>/link/
<client_id>.secret``) written with ``0600`` permissions. It is never
placed in the JSON, never logged, and never included in ``to_dict()``.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Execution modes (requirement 2): LOCAL | SERVER | HYBRID.
EXECUTION_MODES = ("local", "server", "hybrid")
DEFAULT_MODE = "hybrid"

CONFIG_VERSION = 1

_CLIENT_ID_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789-")


class ConfigError(ValueError):
    """Invalid client configuration (fail closed, actionable message)."""


@dataclass
class ReconnectPolicy:
    """Automatic reconnect policy (requirement 1/7).

    Delays follow ``initial_delay * multiplier**attempt`` capped at
    ``max_delay``. ``max_attempts = 0`` means retry forever. Jitter is
    added by the connection manager (seeded RNG) to avoid synchronised
    reconnect storms; the policy itself stays deterministic.
    """

    initial_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    max_attempts: int = 0

    def __post_init__(self) -> None:
        if self.initial_delay <= 0 or self.max_delay <= 0:
            raise ConfigError("reconnect delays must be positive")
        if self.multiplier < 1.0:
            raise ConfigError("reconnect multiplier must be >= 1.0")
        if self.max_attempts < 0:
            raise ConfigError("reconnect max_attempts must be >= 0")

    def delay_for(self, attempt: int) -> float:
        """Deterministic delay before retry number ``attempt`` (0-based)."""
        if attempt < 0:
            attempt = 0
        delay = self.initial_delay * (self.multiplier ** attempt)
        return float(min(delay, self.max_delay))

    def exhausted(self, attempt: int) -> bool:
        if self.max_attempts <= 0:
            return False
        return attempt >= self.max_attempts

    def to_dict(self) -> dict[str, Any]:
        return {"initial_delay": self.initial_delay,
                "max_delay": self.max_delay,
                "multiplier": self.multiplier,
                "max_attempts": self.max_attempts}


@dataclass
class LocalPolicy:
    """What the lightweight client may execute locally (requirements 3, 10).

    The G560 must stay light: local work is capped by task size, and any
    task that requires a model is *never* executed locally (there is no
    local model on the client — by policy, not by accident).
    """

    allow_local: bool = True
    allow_server: bool = True
    #: Refuse LOCAL execution above this requirement length (chars).
    max_task_chars: int = 2000
    #: Refuse LOCAL execution when free RAM is below this (MB).
    min_free_ram_mb: int = 512
    #: Refuse LOCAL execution when the local file walk exceeds this.
    max_files_walked: int = 5000

    def __post_init__(self) -> None:
        if self.max_task_chars <= 0 or self.max_files_walked <= 0:
            raise ConfigError("local limits must be positive")
        if self.min_free_ram_mb < 0:
            raise ConfigError("min_free_ram_mb must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {"allow_local": self.allow_local,
                "allow_server": self.allow_server,
                "max_task_chars": self.max_task_chars,
                "min_free_ram_mb": self.min_free_ram_mb,
                "max_files_walked": self.max_files_walked}


@dataclass
class ClientConfig:
    """All desktop connection settings (requirement 1)."""

    server_url: str = ""
    client_id: str = ""
    project_id: str = ""
    mode: str = DEFAULT_MODE
    #: Per-request timeout in seconds (requirement 1).
    request_timeout: float = 15.0
    #: Heartbeat / health poll interval while connected.
    heartbeat_seconds: float = 10.0
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    local: LocalPolicy = field(default_factory=LocalPolicy)
    #: Directory holding this file + the per-client secret files.
    config_dir: str = ""

    # -- validation ---------------------------------------------------------

    def __post_init__(self) -> None:
        self.server_url = normalize_server_url(self.server_url)
        self.client_id = validate_client_id(self.client_id)
        self.project_id = (self.project_id or "").strip()
        if len(self.project_id) > 64:
            raise ConfigError("project_id too long")
        if self.mode not in EXECUTION_MODES:
            raise ConfigError(
                f"mode must be one of {list(EXECUTION_MODES)}, "
                f"got {self.mode!r}")
        if not (0.5 <= self.request_timeout <= 300.0):
            raise ConfigError("request_timeout must be within 0.5..300s")
        if not (1.0 <= self.heartbeat_seconds <= 3600.0):
            raise ConfigError("heartbeat_seconds must be within 1..3600s")
        if not isinstance(self.reconnect, ReconnectPolicy):
            raise ConfigError("reconnect must be a ReconnectPolicy")
        if not isinstance(self.local, LocalPolicy):
            raise ConfigError("local must be a LocalPolicy")
        self.config_dir = self.config_dir or str(default_config_dir())

    # -- secret handling ------------------------------------------------------

    def secret_path(self) -> Path:
        return Path(self.config_dir) / "link" / f"{self.client_id}.secret"

    def load_secret(self) -> str:
        """Read the client secret from its 0600 file. Never logged.

        On POSIX a group/world-readable secret file is refused with a
        fix-it message (and tightened when possible) — a loose secret
        must not fail silently open.
        """
        path = self.secret_path()
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(
                f"client secret not found at {path} "
                f"(run the setup step on the server: python -m forge.server "
                f"add-client {self.client_id or '<id>'} ...)") from exc
        if not raw:
            raise ConfigError(f"client secret file is empty: {path}")
        _verify_private_permissions(path)
        return raw

    def store_secret(self, secret: str) -> Path:
        """Persist the one-time secret with 0600 permissions."""
        from forge.link.protocol import require_secret
        require_secret(secret)
        path = self.secret_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n", encoding="utf-8")
        _restrict_permissions(path)
        return path

    # -- (de)serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Settings snapshot. **Never contains the secret.**"""
        return {
            "version": CONFIG_VERSION,
            "server_url": self.server_url,
            "client_id": self.client_id,
            "project_id": self.project_id,
            "mode": self.mode,
            "request_timeout": self.request_timeout,
            "heartbeat_seconds": self.heartbeat_seconds,
            "reconnect": self.reconnect.to_dict(),
            "local": self.local.to_dict(),
        }

    def settings_path(self) -> Path:
        return Path(self.config_dir) / "desktop_client.json"

    def save(self) -> Path:
        path = self.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        text = json.dumps(payload, indent=2, sort_keys=True)
        # Runtime invariant (not an assert: it must hold under -O too).
        if "secret" in text.replace("desktop_client", ""):
            raise ConfigError(
                "refusing to save: settings would contain secret material")
        path.write_text(text, encoding="utf-8")
        _restrict_permissions(path)
        return path

    @classmethod
    def from_dict(cls, payload: dict[str, Any],
                  *, config_dir: str = "") -> "ClientConfig":
        if not isinstance(payload, dict):
            raise ConfigError("config payload must be an object")
        reconnect = payload.get("reconnect") or {}
        local = payload.get("local") or {}
        try:
            return cls(
                server_url=str(payload.get("server_url", "")),
                client_id=str(payload.get("client_id", "")),
                project_id=str(payload.get("project_id", "")),
                mode=str(payload.get("mode", DEFAULT_MODE)),
                request_timeout=float(payload.get("request_timeout", 15.0)),
                heartbeat_seconds=float(
                    payload.get("heartbeat_seconds", 10.0)),
                reconnect=ReconnectPolicy(
                    initial_delay=float(reconnect.get("initial_delay", 1.0)),
                    max_delay=float(reconnect.get("max_delay", 30.0)),
                    multiplier=float(reconnect.get("multiplier", 2.0)),
                    max_attempts=int(reconnect.get("max_attempts", 0))),
                local=LocalPolicy(
                    allow_local=bool(local.get("allow_local", True)),
                    allow_server=bool(local.get("allow_server", True)),
                    max_task_chars=int(local.get("max_task_chars", 2000)),
                    min_free_ram_mb=int(local.get("min_free_ram_mb", 512)),
                    max_files_walked=int(
                        local.get("max_files_walked", 5000))),
                config_dir=config_dir or str(payload.get("config_dir", ""))
                or str(default_config_dir()),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid config value: {exc}") from None

    @classmethod
    def load(cls, config_dir: str = "") -> "ClientConfig":
        directory = config_dir or str(default_config_dir())
        path = Path(directory) / "desktop_client.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(
                f"no client configuration at {path} (configure the "
                "connection in Forge Desktop > Server first)") from None
        except (OSError, ValueError) as exc:
            raise ConfigError(f"unreadable client configuration: {exc}") \
                from None
        return cls.from_dict(payload, config_dir=directory)


# -- helpers -----------------------------------------------------------------

def default_config_dir() -> Path:
    return Path.home() / ".forge"


def normalize_server_url(url: str) -> str:
    """Validate + normalize the server URL (fail closed).

    - only ``http``/``https`` schemes;
    - no embedded credentials (``user:pass@host`` is rejected — plaintext
      credentials are never placed in URLs);
    - no query strings or fragments;
    - trailing slash stripped.
    """
    url = (url or "").strip()
    if not url:
        raise ConfigError("server_url is required")
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        raise ConfigError(
            "server_url must not contain credentials (user:pass@host)")
    if "?" in url or "#" in url:
        raise ConfigError("server_url must not contain a query or fragment")
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ConfigError("server_url must start with http:// or https://")
    normalized = url.rstrip("/")
    host_part = normalized.split("//", 1)[-1].split("/", 1)[0]
    if not host_part:
        raise ConfigError("server_url has no host")
    return normalized


def validate_client_id(client_id: str) -> str:
    client_id = (client_id or "").strip().lower()
    if not client_id:
        raise ConfigError("client_id is required")
    if len(client_id) > 32:
        raise ConfigError("client_id too long (max 32)")
    if any(ch not in _CLIENT_ID_ALPHABET for ch in client_id):
        raise ConfigError(
            "client_id may contain lowercase letters, digits and '-'")
    return client_id


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600 (POSIX); a no-op where chmod semantics differ."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _verify_private_permissions(path: Path) -> None:
    """Refuse group/world-readable secret files on POSIX (fail closed).

    Non-POSIX platforms (Windows ACLs) are skipped honestly rather than
    pretending the check ran.
    """
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return  # unreadable -> load_secret already raises a clear error
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        # Tighten first (stop further exposure), then still refuse: the
        # secret may already have been read by another local user, and
        # silently accepting it would hide that fact.
        _restrict_permissions(path)
        client_id = path.stem  # the file is named <client_id>.secret
        raise ConfigError(
            f"client secret file {path} was group/world-readable "
            "(permissions tightened to 0600, but it may have been "
            "exposed) - rotate it on the server with: python -m "
            f"forge.server rotate-secret {client_id}")
