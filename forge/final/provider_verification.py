"""Explicit provider capability verification for the A80 final gate.

Configuration alone never proves capability: ``OPENAI_API_KEY`` being
present does not make a provider AVAILABLE, let alone VERIFIED. A
provider qualifies as production-capable only after an explicit,
successful capability verification that is recent enough under the
documented TTL policy.

The final gate itself stays deterministic and offline — it never
performs network calls. Verification is an explicit operator action:

- ``run_verifications(store, names=None)`` — perform real, bounded
  capability checks and persist the machine-readable results.
- HTTP API: ``POST /api/v1/final/gate/verify``
- Gate evidence: ``provider_report[*].verification`` + the
  ``PROVIDER_STATUS_LADDER`` vocabulary (see
  ``forge.security.provider_states``).

Every verification result is redacted: evidence never contains
credentials, tokens, or secret material, and errors are truncated.
Failures are recorded as failures — never as success.
"""
from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from forge.compute.remote import RemoteComputeError
from forge.security.provider_states import (
    PROVIDER_STATUS_LADDER,
    ProviderState,
    ProviderStatus,
    classify_http_status,
)

#: Default freshness window: a VERIFIED result older than this is
#: ``STALE`` and no longer counts toward PRODUCTION_READY.
DEFAULT_VERIFICATION_TTL = 12 * 3600.0

#: OpenAI-backed providers share one auth/connectivity verification.
OPENAI_BACKED = (
    "model-openai", "research-web", "vision-openai", "voice-whisper",
    "voice-tts", "collaboration-openai", "training-openai",
)

KNOWN_PROVIDERS = (
    "model-openai", "ollama-local", "research-web", "vision-openai",
    "voice-whisper", "voice-tts", "compute-ssh", "compute-colab",
    "compute-modal", "deploy-ssh-rsync", "collaboration-openai",
    "training-openai",
)

_OPENAI_MODEL = os.environ.get("FORGE_VERIFY_MODEL", "gpt-4o-mini")
_OPENAI_TIMEOUT = 20.0
_SSH_TIMEOUT = 20.0
_COLAB_TIMEOUT = 25.0
_OLLAMA_TIMEOUT = 5.0
_MAX_EVIDENCE = 4 * 1024


def verification_ttl() -> float:
    """TTL policy for verification evidence (operator-tunable)."""
    raw = os.environ.get("FORGE_PROVIDER_VERIFICATION_TTL", "").strip()
    if not raw:
        return DEFAULT_VERIFICATION_TTL
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_VERIFICATION_TTL
    return value if value > 0 else DEFAULT_VERIFICATION_TTL


def _env(*names: str) -> str:
    return next((os.environ.get(name, "").strip()
                 for name in names if os.environ.get(name, "").strip()),
                "")


def provider_config(name: str) -> dict[str, Any]:
    """Configuration state of one provider (env-derived, offline).

    Returns ``config_state`` NOT_CONFIGURED / CONFIGURED /
    MISCONFIGURED plus a human ``note``. This is the single source of
    truth for both the gate report and the verification runner.
    """
    openai_key = _env("OPENAI_API_KEY")
    # Voice providers select an engine first: choosing the OpenAI
    # engine without the shared key is MISCONFIGURED, not merely
    # NOT_CONFIGURED. Handled before the generic OPENAI_BACKED branch.
    if name == "voice-whisper":
        stt = _env("FORGE_VOICE_STT_PROVIDER")
        if stt != "openai-whisper":
            return {"kind": "voice", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "set FORGE_VOICE_STT_PROVIDER=openai-whisper"}
        if not openai_key:
            return {"kind": "voice", "configured": True,
                    "config_state": "MISCONFIGURED",
                    "note": "openai-whisper selected but OPENAI_API_KEY "
                            "missing"}
        return {"kind": "voice", "configured": True,
                "config_state": "CONFIGURED",
                "note": "openai-whisper selected + OPENAI_API_KEY present"}
    if name == "voice-tts":
        tts = _env("FORGE_VOICE_TTS_PROVIDER")
        if tts != "openai-tts":
            return {"kind": "voice", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "set FORGE_VOICE_TTS_PROVIDER=openai-tts"}
        if not openai_key:
            return {"kind": "voice", "configured": True,
                    "config_state": "MISCONFIGURED",
                    "note": "openai-tts selected but OPENAI_API_KEY missing"}
        return {"kind": "voice", "configured": True,
                "config_state": "CONFIGURED",
                "note": "openai-tts selected + OPENAI_API_KEY present"}
    if name in OPENAI_BACKED:
        if not openai_key:
            return {"kind": "external", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "set OPENAI_API_KEY for real provider calls"}
        return {"kind": "external", "configured": True,
                "config_state": "CONFIGURED",
                "note": "OPENAI_API_KEY present; capability not yet "
                        "verified"}
    if name == "ollama-local":
        url = _env("OLLAMA_BASE_URL", "OLLAMA_URL")
        return {"kind": "local", "configured": True,
                "config_state": "CONFIGURED",
                "note": ("Local endpoint (implicit default "
                         f"{url or 'http://127.0.0.1:11434'}); excluded "
                         "from external production-ready criteria; "
                         "reachability probed per call")}
    if name == "compute-ssh":
        host = _env("FORGE_COMPUTE_SSH_HOST")
        allowlist = _env("FORGE_COMPUTE_SSH_ALLOWLIST")
        if not host:
            return {"kind": "compute", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "no FORGE_COMPUTE_SSH_HOST configured"}
        if not allowlist:
            return {"kind": "compute", "configured": True,
                    "config_state": "MISCONFIGURED",
                    "note": "FORGE_COMPUTE_SSH_ALLOWLIST missing; "
                            "destination refused (fail-closed)"}
        return {"kind": "compute", "configured": True,
                "config_state": "CONFIGURED",
                "note": "FORGE_COMPUTE_SSH_HOST + allowlist present"}
    if name == "compute-colab":
        colab = _env("FORGE_COLAB_URL")
        if not colab:
            return {"kind": "compute", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "no FORGE_COLAB_URL configured"}
        return {"kind": "compute", "configured": True,
                "config_state": "CONFIGURED",
                "note": "FORGE_COLAB_URL present"}
    if name == "compute-modal":
        token = _env("MODAL_TOKEN_ID")
        if not token:
            return {"kind": "compute", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "no MODAL_TOKEN_ID configured"}
        return {"kind": "compute", "configured": True,
                "config_state": "CONFIGURED",
                "note": "MODAL_TOKEN_ID present"}
    if name == "deploy-ssh-rsync":
        host = _env("FORGE_DEPLOY_SSH_HOST")
        allowlist = _env("FORGE_DEPLOY_SSH_ALLOWLIST")
        if not host:
            return {"kind": "deployment", "configured": False,
                    "config_state": "NOT_CONFIGURED",
                    "note": "no FORGE_DEPLOY_SSH_HOST configured"}
        if not allowlist:
            return {"kind": "deployment", "configured": True,
                    "config_state": "MISCONFIGURED",
                    "note": "FORGE_DEPLOY_SSH_ALLOWLIST missing; "
                            "destination refused (fail-closed)"}
        return {"kind": "deployment", "configured": True,
                "config_state": "CONFIGURED",
                "note": "FORGE_DEPLOY_SSH_HOST/PATH + allowlist present"}
    raise ValueError(f"Unknown provider: {name!r}")


# ---------------------------------------------------------------------------
# minimal real probes
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict[str, Any], headers: dict[str, str],
               timeout: float) -> tuple[str, dict[str, Any] | None]:
    """POST JSON and return ``(state, parsed_json_or_None)``.

    Module-level seam so tests can stub the network; the real
    implementation uses the standard library with strict timeouts and
    a bounded response body.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
        data = json.loads(raw.decode("utf-8"))
        return "SUCCESS", data
    except urllib.error.HTTPError as exc:
        return classify_http_status(exc.code).value, None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)) or \
                "timed out" in str(reason):
            return "TIMEOUT", None
        return "UNAVAILABLE", None
    except TimeoutError:
        return "TIMEOUT", None
    except (ValueError, OSError):
        return "PROVIDER_ERROR", None


def _openai_verify() -> dict[str, Any]:
    """Minimal real OpenAI capability check (auth + connectivity +
    one tiny completion). Returns a redacted evidence dict."""
    key = _env("OPENAI_API_KEY")
    if not key:
        return {"state": "NOT_CONFIGURED",
                "error": "OPENAI_API_KEY is not set"}
    started = time.monotonic()
    state, data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        {"model": _OPENAI_MODEL, "max_tokens": 4,
         "temperature": 0,
         "messages": [{"role": "user",
                       "content": "Reply with the single word: pong"}]},
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {key}"},
        _OPENAI_TIMEOUT)
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    if state != "SUCCESS":
        return {"state": state,
                "error": f"OpenAI verification request failed ({state})",
                "evidence": {"provider": "openai", "model": _OPENAI_MODEL,
                             "http_status": None,
                             "latency_ms": latency_ms,
                             "scope": "auth+connectivity"}}
    ok = isinstance(data, dict) and bool(data.get("choices"))
    return {
        "state": "VERIFIED" if ok else "PROVIDER_ERROR",
        "error": "" if ok else "OpenAI response carried no choices",
        "evidence": {"provider": "openai", "model": _OPENAI_MODEL,
                     "http_status": 200, "latency_ms": latency_ms,
                     "scope": ("auth+connectivity+minimal completion; "
                               "media/upload capabilities are not "
                               "exercised by this check"),
                     "ok": ok},
    }


def _ollama_verify() -> dict[str, Any]:
    """Probe the local Ollama endpoint (/api/tags)."""
    import urllib.error
    import urllib.request

    base = (_env("OLLAMA_BASE_URL", "OLLAMA_URL")
            or "http://127.0.0.1:11434").rstrip("/")
    url = base + "/api/tags"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_TIMEOUT) as resp:
            raw = resp.read(512_000)
        data = json.loads(raw.decode("utf-8"))
        models = sorted(model.get("name", "")
                        for model in (data.get("models") or []))
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        return {"state": "VERIFIED",
                "error": "",
                "evidence": {"provider": "ollama", "endpoint": base,
                             "models": models[:10],
                             "model_count": len(models),
                             "latency_ms": latency_ms,
                             "scope": "local endpoint reachability"}}
    except urllib.error.HTTPError as exc:
        return {"state": "PROVIDER_ERROR",
                "error": f"Ollama HTTP {exc.code}",
                "evidence": {"provider": "ollama", "endpoint": base}}
    except Exception as exc:  # noqa: BLE001 — transport catch-all
        message = str(exc)
        timed_out = isinstance(exc, TimeoutError) or "timed out" in message
        return {"state": "TIMEOUT" if timed_out else "UNAVAILABLE",
                "error": ("Ollama endpoint unreachable" if not timed_out
                          else "Ollama endpoint timed out"),
                "evidence": {"provider": "ollama", "endpoint": base}}


def _ssh_verify() -> dict[str, Any]:
    """Real SSH transport probe (bounded, strict host verification).

    The probe reuses the production transport: allowlist + known_hosts
    enforcement + stdin code transport. Failures are classified:
    known-host verification failures fail closed.
    """
    try:
        from forge.compute.remote import _execute_ssh_secure, \
            parse_ssh_config, ssh_destination_policy
    except Exception as exc:  # pragma: no cover — import guard
        return {"state": "PROVIDER_ERROR",
                "error": f"SSH transport unavailable: {exc}",
                "evidence": {"provider": "ssh-remote"}}
    try:
        config = parse_ssh_config()
    except Exception as exc:
        return {"state": "MISCONFIGURED",
                "error": f"SSH configuration refused: {exc}",
                "evidence": {"provider": "ssh-remote"}}
    try:
        # Destination-IP policy (resolved-address classification) applies
        # to verification too: an allowlisted name that resolves into a
        # private/metadata range is refused before the probe connects.
        ssh_destination_policy(config)
    except RemoteComputeError as exc:
        return {"state": "POLICY_DENIED",
                "error": str(exc),
                "evidence": {"provider": "ssh-remote",
                             "target": str(getattr(config, "target", ""))}}
    outcome = _execute_ssh_secure(
        "import sys; sys.stdout.write('forge-verify-ok')",
        config, _SSH_TIMEOUT)
    status = outcome.get("status", "failed")
    output = (outcome.get("output") or "")[:400]
    evidence: dict[str, Any] = {
        "provider": "ssh-remote",
        "target": str(getattr(config, "target", "")),
        "elapsed_ms": outcome.get("elapsed_ms", 0.0),
    }
    if status == "succeeded":
        return {"state": "VERIFIED", "error": "", "evidence": evidence}
    if status == "timeout":
        return {"state": "TIMEOUT",
                "error": "SSH verification timed out",
                "evidence": evidence}
    lowered = output.lower()
    if "host key verification failed" in lowered or \
            "identification has changed" in lowered:
        return {"state": "POLICY_DENIED",
                "error": ("SSH host-key verification failed; connection "
                          "refused (fail-closed)"),
                "evidence": {**evidence, "detail": output[:200]}}
    if "permission denied" in lowered or "authentication failed" in lowered:
        return {"state": "AUTH_ERROR",
                "error": "SSH authentication failed",
                "evidence": {**evidence, "detail": output[:200]}}
    if "could not resolve hostname" in lowered:
        return {"state": "UNAVAILABLE",
                "error": "SSH hostname could not be resolved",
                "evidence": {**evidence, "detail": output[:200]}}
    return {"state": "PROVIDER_ERROR",
            "error": f"SSH verification failed ({status})",
            "evidence": {**evidence, "detail": output[:200]}}


def _colab_verify() -> dict[str, Any]:
    """Real Colab-compatible kernel-proxy probe (bounded)."""
    try:
        from forge.compute.remote import RemoteComputeBackend
    except Exception as exc:  # pragma: no cover
        return {"state": "PROVIDER_ERROR",
                "error": f"Colab transport unavailable: {exc}",
                "evidence": {"provider": "colab"}}
    backend = RemoteComputeBackend()
    if not backend._colab_url:
        return {"state": "NOT_CONFIGURED",
                "error": "FORGE_COLAB_URL is not set",
                "evidence": {"provider": "colab"}}
    outcome = backend.execute("print('forge-verify-ok')",
                              timeout=_COLAB_TIMEOUT)
    status = outcome.get("status", "failed")
    output = (outcome.get("output") or "")[:400]
    evidence: dict[str, Any] = {
        "provider": "colab",
        "elapsed_ms": outcome.get("elapsed_ms", 0.0),
    }
    if status == "succeeded":
        return {"state": "VERIFIED", "error": "", "evidence": evidence}
    if status == "refused":
        return {"state": "POLICY_DENIED",
                "error": output or "Colab endpoint refused by policy",
                "evidence": evidence}
    if status == "timeout":
        return {"state": "TIMEOUT",
                "error": "Colab verification timed out",
                "evidence": evidence}
    lowered = output.lower()
    if "401" in lowered or "403" in lowered or "unauthorized" in lowered:
        return {"state": "AUTH_ERROR",
                "error": "Colab endpoint rejected the request",
                "evidence": {**evidence, "detail": output[:200]}}
    return {"state": "PROVIDER_ERROR",
            "error": f"Colab verification failed ({status})",
            "evidence": {**evidence, "detail": output[:200]}}


def _modal_verify() -> dict[str, Any]:
    """Modal verification is best-effort: the client is an optional
    lazy import, so absence of the SDK is an honest PROVIDER_ERROR."""
    try:
        import modal  # noqa: F401
    except Exception:
        return {"state": "PROVIDER_ERROR",
                "error": ("modal client is not installed in this "
                          "environment; verification unavailable"),
                "evidence": {"provider": "modal"}}
    return {"state": "PROVIDER_ERROR",
            "error": ("Modal verification requires MODAL_TOKEN_SECRET and "
                      "a live Modal workspace; not executed here"),
            "evidence": {"provider": "modal"}}


def _deploy_verify() -> dict[str, Any]:
    """Real deploy-ssh verification: strict config + one bounded
    connectivity/auth round-trip. Never stages an rsync transfer."""
    import subprocess

    try:
        from forge.deployment.production import SSHDeployer, \
            _ssh_base_argv
    except Exception as exc:  # pragma: no cover — import guard
        return {"state": "PROVIDER_ERROR",
                "error": f"Deploy transport unavailable: {exc}",
                "evidence": {"provider": "deploy-ssh-rsync"}}
    deployer = SSHDeployer()
    if not deployer.available():
        return {"state": "MISCONFIGURED",
                "error": deployer.config_error or
                         "deploy SSH configuration refused",
                "evidence": {"provider": "deploy-ssh-rsync"}}
    target = deployer._target
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [*_ssh_base_argv(target), "true"],
            capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return {"state": "TIMEOUT",
                "error": "Deploy SSH verification timed out",
                "evidence": {"provider": "deploy-ssh-rsync",
                             "target": f"{target['user']}@{target['host']}"}}
    evidence = {"provider": "deploy-ssh-rsync",
                "target": f"{target['user']}@{target['host']}",
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    if proc.returncode == 0:
        return {"state": "VERIFIED", "error": "", "evidence": evidence}
    output = ((proc.stderr or "") + (proc.stdout or ""))[:400]
    lowered = output.lower()
    if "host key verification failed" in lowered or \
            "identification has changed" in lowered:
        return {"state": "POLICY_DENIED",
                "error": ("Deploy SSH host-key verification failed; "
                          "connection refused (fail-closed)"),
                "evidence": {**evidence, "detail": output[:200]}}
    if "permission denied" in lowered:
        return {"state": "AUTH_ERROR",
                "error": "Deploy SSH authentication failed",
                "evidence": {**evidence, "detail": output[:200]}}
    if "could not resolve hostname" in lowered:
        return {"state": "UNAVAILABLE",
                "error": "Deploy SSH hostname could not be resolved",
                "evidence": {**evidence, "detail": output[:200]}}
    return {"state": "PROVIDER_ERROR",
            "error": f"Deploy SSH verification failed (exit "
                     f"{proc.returncode})",
            "evidence": {**evidence, "detail": output[:200]}}


def _probe(name: str) -> dict[str, Any]:
    """Run the real probe for one provider; never raises."""
    try:
        if name in OPENAI_BACKED:
            return _openai_verify()
        if name == "ollama-local":
            return _ollama_verify()
        if name == "compute-ssh":
            return _ssh_verify()
        if name == "compute-colab":
            return _colab_verify()
        if name == "compute-modal":
            return _modal_verify()
        if name == "deploy-ssh-rsync":
            # Verification never stages a transfer: it exercises the
            # deploy SSH configuration (allowlist + known_hosts) with
            # one bounded connectivity/auth round-trip. The rsync path
            # itself stays allowlisted and approval-gated.
            return _deploy_verify()
    except Exception as exc:  # noqa: BLE001 — never let a probe raise
        return {"state": "PROVIDER_ERROR",
                "error": f"Verification probe failed: {str(exc)[:300]}",
                "evidence": {"provider": name}}
    return {"state": "NOT_CONFIGURED",
            "error": "provider not configured",
            "evidence": {"provider": name}}


def verify_provider(name: str) -> dict[str, Any]:
    """Verify one provider and return a full verification record."""
    if name not in KNOWN_PROVIDERS:
        raise ValueError(f"Unknown provider: {name!r}")
    cfg = provider_config(name)
    now = time.time()
    base = {
        "provider": name,
        "configured": bool(cfg["configured"]),
        "config_state": cfg["config_state"],
        "capability": {
            "model-openai": "real model completion",
            "research-web": "web search provider auth+connectivity",
            "vision-openai": "vision API auth+connectivity (minimal "
                             "completion; image upload not exercised)",
            "voice-whisper": "STT API auth+connectivity (minimal "
                             "completion; audio upload not exercised)",
            "voice-tts": "TTS API auth+connectivity (minimal completion; "
                         "audio synthesis not exercised)",
            "collaboration-openai": "collaboration API auth+connectivity",
            "training-openai": "training API auth+connectivity (upload "
                               "path separately data-policy gated)",
            "ollama-local": "local endpoint reachability",
            "compute-ssh": "SSH transport: allowlist + known_hosts + "
                           "remote python execution",
            "compute-colab": "Colab-compatible kernel proxy execution",
            "compute-modal": "Modal client availability",
            "deploy-ssh-rsync": "SSH transport verification (deploy "
                                "itself is allowlist + approval gated)",
        }.get(name, "capability verification"),
        "checked_at": now,
        "verified_at": None,
        "evidence": {},
        "error": "",
    }
    if cfg["config_state"] != "CONFIGURED":
        # Never touch the network for a provider that is not fully
        # configured: record the config state and stop.
        base.update({
            "status": cfg["config_state"],  # NOT_CONFIGURED|MISCONFIGURED
            "note": cfg["note"],
        })
        return base
    probe = _probe(name)
    state = probe.get("state", "PROVIDER_ERROR")
    base["evidence"] = probe.get("evidence") or {}
    base["error"] = (probe.get("error") or "")[:_MAX_EVIDENCE]
    if state == "VERIFIED":
        base["status"] = ProviderStatus.VERIFIED.value
        base["verified_at"] = now
    elif state in (ProviderStatus.TIMEOUT.value,
                   ProviderStatus.RATE_LIMITED.value,
                   ProviderStatus.AUTH_ERROR.value,
                   ProviderStatus.POLICY_DENIED.value,
                   ProviderStatus.UNAVAILABLE.value,
                   ProviderStatus.PROVIDER_ERROR.value):
        base["status"] = state
    else:  # pragma: no cover — defensive
        base["status"] = ProviderStatus.PROVIDER_ERROR.value
    return base


def record_status(name: str, verify: dict[str, Any],
                  store: Any) -> dict[str, Any]:
    """Persist one verification record in the store."""
    entry = {
        "provider": name,
        "status": verify.get("status", ProviderStatus.NOT_CONFIGURED.value),
        "configured": bool(verify.get("configured")),
        "config_state": verify.get("config_state", ""),
        "capability": verify.get("capability", ""),
        "checked_at": verify.get("checked_at", time.time()),
        "verified_at": verify.get("verified_at"),
        "evidence": verify.get("evidence") or {},
        "error": verify.get("error", ""),
        "note": verify.get("note", ""),
    }
    store.save(name, entry)
    return entry


def run_verifications(store: Any, names: list[str] | None = None,
                      ) -> dict[str, Any]:
    """Explicitly verify providers and persist the results.

    ``names=None`` verifies every provider that is currently
    configured. Unconfigured providers are recorded as
    NOT_CONFIGURED without any network activity. Never raises: every
    probe outcome is recorded.
    """
    if names is None:
        # Verify every known provider: configured ones are probed for
        # real; the rest are recorded NOT_CONFIGURED (no network).
        names = list(KNOWN_PROVIDERS)
    results: dict[str, Any] = {}
    for name in names:
        try:
            verify = verify_provider(name)
        except Exception as exc:  # noqa: BLE001
            verify = {
                "provider": name, "status": "PROVIDER_ERROR",
                "configured": True, "config_state": "CONFIGURED",
                "checked_at": time.time(), "verified_at": None,
                "evidence": {}, "note": "",
                "error": f"verification crashed: {str(exc)[:300]}",
            }
        results[name] = record_status(name, verify, store)
    return results


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

class ProviderVerificationStore:
    """SQLite-backed records of provider verification attempts."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS provider_verifications (\n"
            "  provider TEXT PRIMARY KEY,\n"
            "  status TEXT NOT NULL,\n"
            "  configured INTEGER NOT NULL,\n"
            "  config_state TEXT NOT NULL,\n"
            "  capability TEXT NOT NULL DEFAULT '',\n"
            "  checked_at REAL NOT NULL,\n"
            "  verified_at REAL,\n"
            "  evidence TEXT NOT NULL DEFAULT '{}',\n"
            "  error TEXT NOT NULL DEFAULT '',\n"
            "  note TEXT NOT NULL DEFAULT ''\n"
            ")")

    def save(self, provider: str, record: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO provider_verifications (\n"
            "  provider, status, configured, config_state, capability,\n"
            "  checked_at, verified_at, evidence, error, note)\n"
            "  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider, record.get("status", "PROVIDER_ERROR"),
             int(bool(record.get("configured"))),
             record.get("config_state", ""),
             record.get("capability", ""),
             record.get("checked_at", time.time()),
             record.get("verified_at"),
             json.dumps(record.get("evidence") or {}),
             record.get("error", ""),
             record.get("note", "")))

    def get(self, provider: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM provider_verifications WHERE provider = ?",
            (provider,)).fetchone()
        return self._row(row) if row else None

    def all(self) -> dict[str, dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM provider_verifications").fetchall()
        return {row["provider"]: self._row(row) for row in rows}

    def _row(self, row: Any) -> dict[str, Any]:
        try:
            evidence = json.loads(row["evidence"] or "{}")
        except ValueError:
            evidence = {}
        return {
            "provider": row["provider"],
            "status": row["status"],
            "configured": bool(row["configured"]),
            "config_state": row["config_state"],
            "capability": row["capability"],
            "checked_at": row["checked_at"],
            "verified_at": row["verified_at"],
            "evidence": evidence,
            "error": row["error"],
            "note": row["note"],
        }


def is_recent(record: dict[str, Any], ttl: float | None = None) -> bool:
    """True when the record is recent enough under the TTL policy.

    Recency applies to any outcome (VERIFIED or a failure): an
    AUTH_ERROR from an hour ago is still an unresolved failure; a
    VERIFIED result older than the TTL is stale and no longer counts.
    """
    ttl = verification_ttl() if ttl is None else ttl
    stamp = record.get("verified_at") or record.get("checked_at") or 0.0
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return False
    return (time.time() - stamp) <= ttl
