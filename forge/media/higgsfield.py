"""Real client for the Higgsfield generative-media API (stdlib only).

Implements the documented asynchronous lifecycle (docs.higgsfield.ai):
submit ``POST {base}/{model}`` -> ``queued`` with ``request_id``,
``status_url`` and ``cancel_url`` -> poll ``GET status_url`` until a
terminal state (``completed`` / ``failed`` / ``nsfw`` / ``canceled``).

Authentication is ``Authorization: Key {id}:{secret}`` with credentials
from the environment (``HF_API_KEY_ID`` / ``HF_API_KEY_SECRET``, with
``HIGGSFIELD_API_KEY_ID`` / ``HIGGSFIELD_API_KEY_SECRET`` aliases).
Credentials never appear in reprs, errors, or logs; only a redacted
fingerprint is exposed.

Retry behavior follows the documented safe policy: ``GET`` status
requests retry on network failure and ``5xx`` with exponential backoff
and jitter inside a bounded deadline; ``POST`` submissions are never
auto-retried after an ambiguous timeout (no idempotency key exists).
Every response's ``X-Correlation-ID`` is captured for support tickets.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.higgsfield.ai"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "forge-ai/0.1.0 (higgsfield-client)"
CORRELATION_HEADER = "X-Correlation-ID"

#: Model paths confirmed by the official docs. Any other documented
#: model path may be passed to :meth:`HiggsfieldClient.submit` directly;
#: unknown strings are never guessed here.
KNOWN_MODELS = {
    "soul-standard-image": "higgsfield-ai/soul/v2/standard",
}

TERMINAL_STATUSES = frozenset({"completed", "failed", "nsfw", "canceled"})
NON_TERMINAL_STATUSES = frozenset({"queued", "in_progress"})

_MAX_BODY_BYTES = 1_000_000
_MAX_DOWNLOAD_BYTES = 500_000_000


class HiggsfieldError(Exception):
    """Base error; always carries the correlation id when the API sent one."""

    def __init__(self, message: str, *, correlation_id: str = "") -> None:
        super().__init__(message)
        self.correlation_id = correlation_id


class HiggsfieldConfigurationError(HiggsfieldError):
    """Missing credentials or invalid client-side arguments (never raised
    for server responses)."""


class HiggsfieldAuthError(HiggsfieldError):
    """HTTP 401: missing or invalid credentials (do not retry as-is)."""


class HiggsfieldCreditsError(HiggsfieldError):
    """HTTP 403: insufficient credits."""


class HiggsfieldNotFoundError(HiggsfieldError):
    """HTTP 404: request or model not found for this account."""


class HiggsfieldValidationError(HiggsfieldError):
    """HTTP 400/422: the request itself is wrong (do not retry as-is)."""


class HiggsfieldRetryableError(HiggsfieldError):
    """HTTP 423/429/5xx or network failure: safe to retry a GET later."""


class HiggsfieldTimeoutError(HiggsfieldError):
    """Polling deadline expired before a terminal state."""


def known_models() -> dict[str, str]:
    """Return the documented model alias -> API path mapping."""
    return dict(KNOWN_MODELS)


@dataclass(frozen=True)
class HiggsfieldCredentials:
    """Server-side API credential pair (id + secret)."""

    key_id: str
    secret: str

    def __post_init__(self) -> None:
        if not self.key_id or not self.secret:
            raise HiggsfieldConfigurationError(
                "Higgsfield needs both a key id and a secret.")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HiggsfieldCredentials | None:
        """Load credentials from the environment; None when absent.

        Primary names are the official ``HF_API_KEY_ID`` /
        ``HF_API_KEY_SECRET``; ``HIGGSFIELD_API_KEY_ID`` /
        ``HIGGSFIELD_API_KEY_SECRET`` work as aliases.
        """
        source = env if env is not None else os.environ
        key_id = (source.get("HF_API_KEY_ID")
                  or source.get("HIGGSFIELD_API_KEY_ID") or "").strip()
        secret = (source.get("HF_API_KEY_SECRET")
                  or source.get("HIGGSFIELD_API_KEY_SECRET") or "").strip()
        if not key_id or not secret:
            return None
        return cls(key_id=key_id, secret=secret)

    @property
    def auth_header(self) -> str:
        return f"Key {self.key_id}:{self.secret}"

    def fingerprint(self) -> str:
        """Redacted identifier safe for logs (last 4 chars of the id)."""
        return f"key …{self.key_id[-4:]}" if len(self.key_id) > 4 else "key (short id)"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"HiggsfieldCredentials({self.fingerprint()})"


@dataclass(frozen=True)
class Submission:
    """Accepted generation request (initial ``queued`` response)."""

    request_id: str
    status: str
    status_url: str
    cancel_url: str = ""
    correlation_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationStatus:
    """One status snapshot; outputs are populated when completed."""

    status: str
    request_id: str
    images: tuple[dict[str, Any], ...] = ()
    video: dict[str, Any] | None = None
    audios: tuple[dict[str, Any], ...] = ()
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    correlation_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    def output_urls(self) -> list[str]:
        """Flatten every output URL (images, video, audio, artifacts)."""
        urls: list[str] = []
        for image in self.images:
            url = image.get("url")
            if url:
                urls.append(str(url))
        if self.video and self.video.get("url"):
            urls.append(str(self.video["url"]))
        for audio in self.audios:
            url = audio.get("url")
            if url:
                urls.append(str(url))
        for value in self.artifacts.values():
            if isinstance(value, dict) and value.get("url"):
                urls.append(str(value["url"]))
            elif isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        return urls

    def raise_for_terminal(self) -> GenerationStatus:
        """Return self when completed; raise describing failed/nsfw/canceled."""
        if self.status == "completed":
            return self
        raise HiggsfieldError(
            f"Generation {self.request_id} ended with status "
            f"{self.status!r}: {self.error or 'no detail'}",
            correlation_id=self.correlation_id)


class HiggsfieldClient:
    """Blocking Higgsfield API client over stdlib urllib."""

    def __init__(self, credentials: HiggsfieldCredentials | None = None, *,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        if timeout <= 0:
            raise HiggsfieldConfigurationError("timeout must be positive")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HiggsfieldConfigurationError(
                f"Invalid base URL: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials or HiggsfieldCredentials.from_env()
        self.timeout = float(timeout)

    @property
    def configured(self) -> bool:
        return self.credentials is not None

    def require_credentials(self) -> HiggsfieldCredentials:
        if self.credentials is None:
            raise HiggsfieldConfigurationError(
                "Higgsfield credentials are not configured; set "
                "HF_API_KEY_ID and HF_API_KEY_SECRET.")
        return self.credentials

    # -- submissions --------------------------------------------------------

    def submit(self, model: str, params: dict[str, Any], *,
               timeout: float | None = None) -> Submission:
        """Submit a generation; never auto-retries the POST."""
        creds = self.require_credentials()
        if not isinstance(model, str) or not model.strip():
            raise HiggsfieldConfigurationError("model must be a non-empty path")
        if not isinstance(params, dict) or not params:
            raise HiggsfieldConfigurationError("params must be a non-empty dict")
        model_path = KNOWN_MODELS.get(model.strip(), model.strip()).strip("/")
        url = f"{self.base_url}/{model_path}"
        payload = json.dumps(params).encode("utf-8")
        data, correlation = self._request(
            "POST", url, creds, payload, timeout=timeout)
        try:
            request_id = str(data["request_id"])
            status = str(data.get("status", "queued"))
            status_url = str(data["status_url"])
            cancel_url = str(data.get("cancel_url", ""))
        except (KeyError, TypeError) as exc:
            raise HiggsfieldError(
                f"Higgsfield returned an unparseable submission: {exc}",
                correlation_id=correlation) from exc
        if not request_id or not status_url:
            raise HiggsfieldError(
                "Higgsfield submission is missing request_id/status_url",
                correlation_id=correlation)
        return Submission(request_id=request_id, status=status,
                          status_url=status_url, cancel_url=cancel_url,
                          correlation_id=correlation, raw=data)

    def generate_image(self, prompt: str, **params: Any) -> Submission:
        """Submit a Soul standard image generation (documented path)."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise HiggsfieldConfigurationError("prompt must be non-empty")
        body: dict[str, Any] = {"prompt": prompt.strip()}
        body.update(params)
        return self.submit("soul-standard-image", body)

    # -- status / waiting ----------------------------------------------------

    def get_status(self, request: Submission | str, *,
                   attempts: int = 3,
                   deadline: float = 60.0) -> GenerationStatus:
        """Fetch one status snapshot, retrying GETs per the safe policy."""
        if attempts < 1:
            raise HiggsfieldConfigurationError("attempts must be at least 1")
        if deadline <= 0:
            raise HiggsfieldConfigurationError("deadline must be positive")
        creds = self.require_credentials()
        url = (request.status_url if isinstance(request, Submission)
               else self._status_url(request))
        started = time.monotonic()
        last_error: HiggsfieldError | None = None
        for attempt in range(attempts):
            try:
                data, correlation = self._request(
                    "GET", url, creds, None, timeout=self.timeout)
            except HiggsfieldRetryableError as exc:
                last_error = exc
            else:
                return self._parse_status(data, correlation)
            delay = min(2.0 ** attempt + random.uniform(0, 1.0), 30.0)
            if time.monotonic() - started + delay > deadline:
                break
            time.sleep(delay)
        raise (last_error or HiggsfieldRetryableError(
            f"Status fetch for {url} failed; no response captured."))

    def wait(self, request: Submission | str, *, timeout: float = 600.0,
             interval: float = 3.0,
             status_attempts: int = 3) -> GenerationStatus:
        """Poll until a terminal state or the timeout expires."""
        if timeout <= 0:
            raise HiggsfieldConfigurationError("timeout must be positive")
        if interval <= 0:
            raise HiggsfieldConfigurationError("interval must be positive")
        target = request
        started = time.monotonic()
        while True:
            snapshot = self.get_status(
                target, attempts=status_attempts,
                deadline=max(5.0, timeout - (time.monotonic() - started)))
            if snapshot.terminal:
                return snapshot
            if time.monotonic() - started >= timeout:
                raise HiggsfieldTimeoutError(
                    f"Generation {snapshot.request_id} still "
                    f"{snapshot.status!r} after {timeout:.0f}s",
                    correlation_id=snapshot.correlation_id)
            target = snapshot.request_id
            remaining = timeout - (time.monotonic() - started)
            time.sleep(max(0.05, min(interval, remaining)))
            if time.monotonic() - started >= timeout:
                raise HiggsfieldTimeoutError(
                    f"Generation {snapshot.request_id} still "
                    f"{snapshot.status!r} after {timeout:.0f}s",
                    correlation_id=snapshot.correlation_id)

    def cancel(self, request: Submission | str) -> bool:
        """Cancel a queued request; True when accepted (202).

        Returns False when the API reports the request already started
        (HTTP 400); other failures raise.
        """
        creds = self.require_credentials()
        if isinstance(request, Submission):
            url = request.cancel_url or self._cancel_url(request.request_id)
        else:
            url = self._cancel_url(request)
        http_request = urllib.request.Request(
            url, data=b"", method="POST", headers=self._headers(creds))
        try:
            with urllib.request.urlopen(
                    http_request, timeout=self.timeout) as response:
                return response.status in (200, 202, 204)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                return False
            raise self._translate(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HiggsfieldRetryableError(
                f"Higgsfield cancel failed (network): {exc}") from exc

    # -- downloads ------------------------------------------------------------

    def download(self, url: str, dest_dir: str | Path, *,
                 filename: str | None = None,
                 timeout: float | None = None) -> Path:
        """Download a completed output URL into ``dest_dir`` (confined)."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HiggsfieldConfigurationError(
                f"Refusing to download non-HTTP URL: {url!r}")
        root = Path(dest_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        name = filename or Path(parsed.path).name or "output.bin"
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise HiggsfieldConfigurationError(
                f"Unsafe download filename: {name!r}")
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HiggsfieldConfigurationError(
                f"Download escapes destination: {name!r}") from exc
        http_request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(
                    http_request,
                    timeout=self.timeout if timeout is None else timeout
                    ) as response, open(target, "wb") as handle:
                received = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _MAX_DOWNLOAD_BYTES:
                        handle.close()
                        target.unlink(missing_ok=True)
                        raise HiggsfieldError(
                            f"Download exceeds the "
                            f"{_MAX_DOWNLOAD_BYTES} byte cap: {url}")
                    handle.write(chunk)
        except urllib.error.HTTPError as exc:
            raise self._translate(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HiggsfieldRetryableError(
                f"Higgsfield download failed (network): {exc}") from exc
        return target

    # -- internals --------------------------------------------------------------

    def _status_url(self, request_id: str) -> str:
        return self._request_url(request_id, "status")

    def _cancel_url(self, request_id: str) -> str:
        return self._request_url(request_id, "cancel")

    def _request_url(self, request_id: str, action: str) -> str:
        if not isinstance(request_id, str) or not request_id.strip():
            raise HiggsfieldConfigurationError(
                "request_id must be a non-empty string")
        rid = request_id.strip()
        if "/" in rid or "\\" in rid or " " in rid:
            raise HiggsfieldConfigurationError(
                f"Unsafe request_id: {rid!r}")
        return f"{self.base_url}/requests/{rid}/{action}"

    def _headers(self, creds: HiggsfieldCredentials) -> dict[str, str]:
        return {"Authorization": creds.auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT}

    def _request(self, method: str, url: str,
                 creds: HiggsfieldCredentials,
                 payload: bytes | None, *,
                 timeout: float | None) -> tuple[dict[str, Any], str]:
        http_request = urllib.request.Request(
            url, data=payload, method=method, headers=self._headers(creds))
        try:
            with urllib.request.urlopen(
                    http_request,
                    timeout=self.timeout if timeout is None else timeout
                    ) as response:
                correlation = response.headers.get(CORRELATION_HEADER, "")
                raw = response.read(_MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._translate(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HiggsfieldRetryableError(
                f"Higgsfield {method} {url} failed (network): {exc}") from exc
        if len(raw) > _MAX_BODY_BYTES:
            raise HiggsfieldError("Higgsfield response exceeds size cap",
                                  correlation_id=correlation)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HiggsfieldError(
                f"Higgsfield returned invalid JSON: {exc}",
                correlation_id=correlation) from exc
        if not isinstance(data, dict):
            raise HiggsfieldError(
                "Higgsfield returned a non-object response",
                correlation_id=correlation)
        return data, correlation

    def _translate(self, exc: urllib.error.HTTPError) -> HiggsfieldError:
        correlation = ""
        detail = ""
        try:
            correlation = exc.headers.get(CORRELATION_HEADER, "") if exc.headers else ""
            raw = exc.read(_MAX_BODY_BYTES)
            if raw:
                body = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(body, dict):
                    detail = body.get("detail", "")
                    if isinstance(detail, list):
                        detail = "; ".join(str(item) for item in detail[:5])
                    detail = str(detail)
        except Exception:
            detail = ""
        message = f"Higgsfield HTTP {exc.code}"
        if detail:
            message += f": {detail[:300]}"
        elif exc.reason:
            message += f": {exc.reason}"
        if correlation:
            message += f" (correlation {correlation})"
        if exc.code == 401:
            return HiggsfieldAuthError(message, correlation_id=correlation)
        if exc.code == 403:
            return HiggsfieldCreditsError(message, correlation_id=correlation)
        if exc.code == 404:
            return HiggsfieldNotFoundError(message, correlation_id=correlation)
        if exc.code in (400, 422):
            return HiggsfieldValidationError(message, correlation_id=correlation)
        if exc.code in (423, 429) or 500 <= exc.code <= 599:
            return HiggsfieldRetryableError(message, correlation_id=correlation)
        return HiggsfieldError(message, correlation_id=correlation)

    @staticmethod
    def _parse_status(data: dict[str, Any],
                      correlation: str) -> GenerationStatus:
        try:
            status = str(data["status"])
            request_id = str(data.get("request_id", ""))
        except (KeyError, TypeError) as exc:
            raise HiggsfieldError(
                f"Higgsfield returned an unparseable status: {exc}",
                correlation_id=correlation) from exc
        images = data.get("images") or []
        audios = data.get("audios") or []
        if data.get("audio") and data.get("audio") not in audios:
            audios = [*audios, data["audio"]]
        artifacts = {key: value for key, value in data.items()
                     if key in ("zip", "mov", "jsx", "fbx", "ply", "thumbnail")}
        error = data.get("error", "")
        if isinstance(error, dict):
            error = error.get("message", str(error))
        return GenerationStatus(
            status=status, request_id=request_id,
            images=tuple(item for item in images if isinstance(item, dict)),
            video=data.get("video") if isinstance(data.get("video"), dict) else None,
            audios=tuple(item for item in audios if isinstance(item, dict)),
            artifacts=artifacts, error=str(error),
            correlation_id=correlation, raw=data)
