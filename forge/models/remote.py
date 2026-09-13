"""Optional remote provider adapters (Session 11).

Remote inference is **optional, explicit, and never assumed**. A provider is
never reported available just because a credential or a configuration entry
exists: this module keeps four answers separate — ``configured``,
``reachable``, ``verified``, ``ready`` — and only a real probe plus a real
verification produces ``ready``.

Every request goes through the hardened network chain in
:mod:`forge.security.ssrf`:

* HTTPS with real TLS validation (no unverified context, ever);
* destination-IP policy — loopback, private, link-local, CGNAT, multicast and
  reserved ranges are refused, and *every* address a hostname resolves to is
  inspected (so DNS rebinding cannot slip a private address through);
* the connection is pinned to the validated address;
* **redirects are refused** — a 3xx can never retarget a provider request or
  its credentials;
* bounded response size and bounded timeout;
* identity transfer encoding only (no decompression bombs).

Credentials are held in a :class:`RemoteProviderConfig`, are attached to a
request only at send time, and never appear in a log line, an event, an
exception message, or a serialised payload: every error path passes through
:func:`~forge.runtime.model_runtime.redact_text`, and the config's ``to_dict``
reports ``api_key_set`` rather than the key.

The backend answers with the model the endpoint says it used. When that does
not match the identity Forge requested, generation fails as ``MODEL_SPOOFED``
rather than accepting a substitution.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterator, List, Optional, Sequence, Tuple)
from urllib.parse import urlsplit

from forge.runtime.model_runtime import (BackendKind, BackendUnavailableError,
                                         CancellationToken, ErrorKind,
                                         FinishReason, ModelBackend,
                                         RuntimeChunk, RuntimeHealth,
                                         RuntimeModel, RuntimeRequest,
                                         RuntimeResponse, RuntimeState,
                                         RuntimeSecurityError, redact_text)
from forge.security.ssrf import (FetchPolicy, SSRFError, parse_and_validate,
                                 request as hardened_request)

__all__ = [
    "MAX_REMOTE_RESPONSE_BYTES",
    "RemoteHttpBackend",
    "RemoteProviderConfig",
    "RemoteProviderStatus",
    "provider_status",
]

#: Hard ceiling on a completion payload (a completion, not a download).
MAX_REMOTE_RESPONSE_BYTES = 4 * 1024 * 1024
#: Hard ceiling on characters accepted from a stream.
MAX_REMOTE_STREAM_CHARS = 256 * 1024
#: OpenAI-compatible paths. Nothing else is ever requested.
CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"


@dataclass
class RemoteProviderConfig:
    """Explicit configuration for one remote provider.

    ``api_key`` is never serialised, never logged, and never placed in an
    error message. It is read from the environment by :meth:`from_env` so a
    key does not have to appear in a config file.
    """

    provider_id: str
    base_url: str
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    timeout: float = 60.0
    max_response_bytes: int = MAX_REMOTE_RESPONSE_BYTES
    #: Plain http is refused unless the endpoint is loopback *and* this is set.
    allow_http: bool = False
    #: Operator-declared host allowlist; empty means "any public host".
    host_allowlist: Tuple[str, ...] = ()
    #: Extra headers (never credential-bearing ones; those are refused).
    headers: Dict[str, str] = field(default_factory=dict)
    #: Per-request bound on generated characters when streaming.
    max_stream_chars: int = MAX_REMOTE_STREAM_CHARS
    #: Cost posture: a remote provider is never free-first.
    cost_per_token: float = 0.0
    capabilities: Tuple[str, ...] = ()
    context_window: int = 0
    #: The Forge Server endpoint (``network_policy=server-only`` allowance).
    server_endpoint: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id is required")
        self.base_url = _normalize_base_url(self.base_url)
        self.timeout = max(0.5, min(float(self.timeout or 0.5), 600.0))
        self.max_response_bytes = max(
            1024, min(int(self.max_response_bytes or 1024),
                      MAX_REMOTE_RESPONSE_BYTES))
        self.max_stream_chars = max(
            256, min(int(self.max_stream_chars or 256), MAX_REMOTE_STREAM_CHARS))
        self.host_allowlist = tuple(self.host_allowlist or ())
        self.capabilities = tuple(self.capabilities or ())
        for key in list(self.headers):
            if key.lower() in ("authorization", "proxy-authorization",
                               "cookie", "x-api-key", "api-key"):
                # Credential-bearing headers must come from ``api_key``, which
                # is attached at send time and never stored in the config view.
                raise RuntimeSecurityError(
                    "refusing credential-bearing header %r in provider config"
                    % key)

    @property
    def scheme(self) -> str:
        return urlsplit(self.base_url).scheme

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()

    @property
    def loopback(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1")

    def resolved_api_key(self, env: Optional[Dict[str, str]] = None) -> str:
        """The key to send, resolved from config or the declared env var."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            source = os.environ if env is None else env
            return str(source.get(self.api_key_env, "") or "")
        return ""

    @property
    def port(self) -> int:
        """The port in the configured endpoint (0 when it is the default)."""
        try:
            return int(urlsplit(self.base_url).port or 0)
        except ValueError:
            return 0

    @property
    def local_endpoint(self) -> bool:
        """True only for an explicitly enabled loopback http endpoint.

        This is the single opt-in that lets a *private* destination be
        contacted: the operator named the host, named the port and set
        ``allow_http``. An agent-supplied URL never reaches this adapter, and
        nothing else relaxes the destination-IP policy.
        """
        return bool(self.allow_http and self.loopback)

    def fetch_policy(self, *, timeout: Optional[float] = None) -> FetchPolicy:
        """The network policy for every request this provider makes.

        One source of truth for the pre-flight destination check and for the
        actual send, so the two can never disagree. Fail-closed: https only,
        public destinations only, **no redirects**, bounded body.

        Two operator-declared exceptions, both derived from the configured
        base URL (never from request data):

        * an explicit ``allow_http`` on a loopback host tolerates that exact
          host as a private destination (``private_allowed_hosts``);
        * an explicit non-default port is allowed, because refusing it would
          make an operator-configured endpoint unreachable while adding no
          safety — the destination-IP policy still applies.
        """
        port = self.port
        return FetchPolicy(
            allow_http=self.local_endpoint,
            host_allowlist=self.host_allowlist,
            private_allowed_hosts=(self.host,) if self.local_endpoint else (),
            allow_ports=(port,) if port and port not in (80, 443) else (),
            max_redirects=0,          # redirects are refused outright
            timeout=float(timeout) if timeout is not None else self.timeout,
            max_bytes=self.max_response_bytes,
            user_agent="ForgeAI/1.0 (+inference; provider-adapter)",
            audit_operation="remote_inference",
            audit_agent="forge-inference",
        )

    def to_dict(self) -> Dict[str, Any]:
        """A loggable view: no credential material, ever."""
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": bool(self.resolved_api_key()),
            "api_key_env": self.api_key_env,
            "timeout": self.timeout,
            "max_response_bytes": self.max_response_bytes,
            "max_stream_chars": self.max_stream_chars,
            "allow_http": bool(self.allow_http),
            "loopback": self.loopback,
            "port": self.port,
            "local_endpoint": self.local_endpoint,
            "host_allowlist": list(self.host_allowlist),
            "cost_per_token": self.cost_per_token,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
        }

    @classmethod
    def from_env(cls, provider_id: str, *,
                 prefix: str = "FORGE_REMOTE_",
                 env: Optional[Dict[str, str]] = None) -> "RemoteProviderConfig":
        """Build a config from ``FORGE_REMOTE_<PROVIDER>_*`` variables."""
        source = dict(os.environ if env is None else env)
        key = provider_id.upper().replace("-", "_")
        base = "%s%s_" % (prefix, key)

        def value(name: str, default: str = "") -> str:
            return str(source.get(base + name, default) or "")

        url = value("URL")
        if not url:
            raise ValueError(
                "%sURL is required to configure the %r remote provider"
                % (base, provider_id))
        capabilities = tuple(item.strip() for item in
                             value("CAPABILITIES").split(",") if item.strip())
        return cls(
            provider_id=provider_id, base_url=url, model=value("MODEL"),
            api_key_env=value("API_KEY_ENV", base + "API_KEY"),
            timeout=float(value("TIMEOUT", "60") or 60),
            allow_http=value("ALLOW_HTTP", "").lower() in ("1", "true", "yes"),
            host_allowlist=tuple(item.strip() for item in
                                 value("HOST_ALLOWLIST").split(",")
                                 if item.strip()),
            capabilities=capabilities,
            context_window=int(value("CONTEXT_WINDOW", "0") or 0),
            cost_per_token=float(value("COST_PER_TOKEN", "0") or 0),
        )


@dataclass
class RemoteProviderStatus:
    """The four separate answers about a remote provider."""

    provider_id: str
    configured: bool = False
    reachable: bool = False
    verified: bool = False
    ready: bool = False
    endpoint: str = ""
    model: str = ""
    detail: str = ""
    error: str = ""
    latency_ms: float = 0.0
    checked_at: float = field(default_factory=time.time)
    models: List[str] = field(default_factory=list)

    def recompute(self) -> "RemoteProviderStatus":
        self.ready = bool(self.configured and self.reachable and self.verified)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "configured": bool(self.configured),
            "reachable": bool(self.reachable),
            "verified": bool(self.verified),
            "ready": bool(self.ready),
            "endpoint": self.endpoint,
            "model": self.model,
            "detail": self.detail[:400],
            "error": self.error[:400],
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "checked_at": self.checked_at,
            "models": list(self.models)[:64],
        }


def _normalize_base_url(base_url: str) -> str:
    url = str(base_url or "").strip()
    if not url:
        raise ValueError("a remote provider base URL is required")
    lowered = url.lower()
    if not (lowered.startswith("https://") or lowered.startswith("http://")):
        raise RuntimeSecurityError(
            "refusing remote provider URL %r: only https (or explicit "
            "loopback http) is allowed" % url[:200])
    if "@" in url:
        raise RuntimeSecurityError(
            "refusing remote provider URL with embedded credentials; "
            "configure the API key separately")
    while url.endswith("/"):
        url = url[:-1]
    for path in (CHAT_PATH, MODELS_PATH):
        if url.endswith(path):
            url = url[: -len(path)]
            break
    return url


class RemoteHttpBackend(ModelBackend):
    """A real remote provider backend (OpenAI-compatible chat completions).

    Register it with a :class:`~forge.runtime.model_runtime.ModelRuntime`::

        runtime.register_backend(RemoteHttpBackend(config))

    The backend never fabricates output: a refused destination, a failed TLS
    handshake, a 401, a timeout, or a malformed answer is reported as a
    failure with an honest ``error_kind``.
    """

    kind = BackendKind.CUSTOM.value
    local = False
    requires_network = True
    #: A remote provider is never part of the free-first default.
    free = False

    def __init__(self, config: RemoteProviderConfig, *,
                 name: str = "", allow_network: bool = False,
                 audit: Any = None) -> None:
        self.config = config
        self.name = (name or "remote-%s" % config.provider_id).strip()
        self.description = ("Remote provider %r at %s (OpenAI-compatible, "
                            "TLS-validated, redirect-refusing)."
                            % (config.provider_id, config.base_url))
        #: Master switch, mirroring the runtime's ``allow_network`` posture:
        #: nothing is contacted until the operator enables it.
        self.allow_network = bool(allow_network)
        self.audit = audit
        self._reachable: Optional[bool] = None
        self._reachable_detail = ""
        self._verified_models: Dict[str, float] = {}
        self._generations = 0
        self._failures = 0
        self._timeouts = 0
        self._cancellations = 0
        self._blocked = 0

    # -- availability ----------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        if not self.allow_network:
            return (False,
                    "network access is not enabled; the remote provider %s "
                    "was not contacted" % self.config.provider_id)
        reason = self._policy_reason()
        if reason:
            return (False, reason)
        return (True, "endpoint %s (not probed)" % self.config.base_url)

    def _policy_reason(self) -> str:
        """Pre-flight destination policy check (no DNS, no sockets)."""
        config = self.config
        if config.scheme == "http" and not config.local_endpoint:
            return ("plain http is refused for remote provider %s (only "
                    "loopback http may be enabled explicitly)"
                    % config.provider_id)
        try:
            parse_and_validate(config.base_url + MODELS_PATH,
                               config.fetch_policy())
        except SSRFError as exc:
            self._blocked += 1
            return "destination refused by network policy: %s" % exc.reason
        except ValueError as exc:
            return "unusable endpoint: %s" % (exc,)
        return ""

    # -- discovery -------------------------------------------------------

    def list_models(self) -> List[RuntimeModel]:
        data = self._get_json(MODELS_PATH)
        models: List[RuntimeModel] = []
        entries: Sequence[Any] = ()
        if isinstance(data, dict):
            entries = data.get("data") or data.get("models") or []
        elif isinstance(data, list):
            entries = data
        for item in entries:
            name = ""
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                name = str(item.get("id") or item.get("name") or "")
            name = name.strip()
            if not name:
                continue
            models.append(RuntimeModel(
                model_id=RuntimeModel.make_id(self.name, name),
                name=name, backend=self.name, kind="text",
                capabilities=self.config.capabilities,
                context_window=int(self.config.context_window or 0),
                size_bytes=0, format="remote", local=False,
                loaded=False, discovered_at=time.time(),
                metadata={
                    "provider_id": self.config.provider_id,
                    "endpoint": self.config.base_url,
                    "source": "remote-models",
                    "owned_by": str(item.get("owned_by") or "")
                    if isinstance(item, dict) else "",
                    "remote": True,
                }))
        if not models and self.config.model:
            models.append(RuntimeModel(
                model_id=RuntimeModel.make_id(self.name, self.config.model),
                name=self.config.model, backend=self.name, kind="text",
                capabilities=self.config.capabilities,
                context_window=int(self.config.context_window or 0),
                format="remote", local=False, discovered_at=time.time(),
                metadata={"provider_id": self.config.provider_id,
                          "endpoint": self.config.base_url,
                          "source": "configured", "remote": True}))
        models.sort(key=lambda model: model.name)
        return models

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        """Remote models are served by the provider; loading is intent only."""
        loaded = RuntimeModel(
            model_id=model.model_id or RuntimeModel.make_id(self.name,
                                                            model.name),
            name=model.name, backend=self.name, kind=model.kind or "text",
            capabilities=model.capabilities or self.config.capabilities,
            context_window=model.context_window, format="remote",
            local=False, loaded=True, discovered_at=time.time(),
            metadata=dict(model.metadata or {}))
        loaded.metadata["load_note"] = (
            "remote provider serves this model; nothing is loaded locally")
        return loaded

    def unload_model(self, model_id: str) -> bool:
        return True     # nothing resident locally

    # -- inference -------------------------------------------------------

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        started = time.perf_counter()
        if token is not None:
            token.raise_if_cancelled()
        model = self._model_name(request)
        if not model:
            raise BackendUnavailableError(
                "no remote model was requested and none is configured for "
                "provider %s" % self.config.provider_id)
        payload = self._payload(request, model, stream=False)
        outcome = self._send("POST", CHAT_PATH, payload, request)
        if outcome.get("blocked"):
            self._failures += 1
            raise BackendUnavailableError(
                "remote provider %s refused the request: %s"
                % (self.config.provider_id, outcome["error"]))
        status = int(outcome.get("status") or 0)
        if status in (401, 403):
            self._failures += 1
            raise BackendUnavailableError(
                "remote provider %s rejected the credentials (HTTP %d)"
                % (self.config.provider_id, status))
        if not outcome.get("ok"):
            self._failures += 1
            if outcome.get("timeout"):
                self._timeouts += 1
            raise BackendUnavailableError(
                "remote provider %s could not answer: %s"
                % (self.config.provider_id, outcome["error"]))
        try:
            data = json.loads(outcome["body"].decode("utf-8", "replace"))
        except ValueError:
            self._failures += 1
            raise BackendUnavailableError(
                "remote provider %s returned a malformed (non-JSON) answer"
                % self.config.provider_id)
        text, finish, reported_model, usage = _parse_completion(data)
        if not reported_model:
            reported_model = model
        if reported_model != model and not _same_model(reported_model, model):
            self._failures += 1
            return RuntimeResponse.failure(
                "provider answered with model %r but %r was requested "
                "(refusing a silent substitution)"
                % (reported_model[:120], model[:120]),
                kind=ErrorKind.SECURITY.value, backend=self.name, model=model,
                request_id=request.request_id, trace_id=request.trace_id,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                metadata={"reported_model": reported_model[:120]})
        if not text:
            self._failures += 1
            return RuntimeResponse.failure(
                "provider returned an empty completion",
                kind=ErrorKind.PROTOCOL.value, backend=self.name, model=model,
                request_id=request.request_id, trace_id=request.trace_id,
                latency_ms=(time.perf_counter() - started) * 1000.0)
        self._generations += 1
        return RuntimeResponse(
            text=text, model=reported_model, backend=self.name, success=True,
            request_id=request.request_id, trace_id=request.trace_id,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            finish_reason=finish,
            metadata={
                "provider_id": self.config.provider_id,
                "endpoint": self.config.base_url,
                "http_status": status,
                "remote": True,
            })

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        if token is not None:
            token.raise_if_cancelled()
        model = self._model_name(request)
        if not model:
            raise BackendUnavailableError(
                "no remote model was requested and none is configured")
        # The stdlib HTTP client cannot stream a POST incrementally without a
        # socket-level reader, so the adapter performs one bounded request and
        # replays the completion as chunks. That is honest: the chunks are the
        # provider's own text, and ``done`` is only set at the end.
        response = self.generate(request, token=token)
        if not response.success:
            raise BackendUnavailableError(response.error or "stream failed")
        text = response.text or ""
        #: ``max_stream_chars`` bounds how much of the provider's answer may be
        #: replayed, not merely the chunk size: an oversized completion is cut
        #: at the bound and the cut is *reported*, never silently completed.
        limit = max(1, int(self.config.max_stream_chars or 1))
        truncated = len(text) > limit
        send = text[:limit]
        size = max(1, min(64, limit))
        index = 0
        produced = 0
        while produced < len(send):
            if token is not None and token.cancelled:
                self._cancellations += 1
                return
            piece = send[produced:produced + size]
            produced += len(piece)
            index += 1
            yield RuntimeChunk(text=piece, index=index,
                               request_id=request.request_id,
                               output_tokens=produced)
        yield RuntimeChunk(text="", index=index + 1, done=True,
                           request_id=request.request_id,
                           output_tokens=response.output_tokens,
                           finish_reason=("length" if truncated
                                          else response.finish_reason),
                           metadata={"model_id": response.model,
                                     "streamed_by": "chunked-replay",
                                     "truncated": bool(truncated),
                                     "max_stream_chars": limit,
                                     "provider_chars": len(text)})

    # -- ops -------------------------------------------------------------

    def health(self, probe: bool = True) -> RuntimeHealth:
        health = RuntimeHealth(
            backend=self.name, kind=self.kind,
            status=RuntimeState.UNKNOWN.value, checked_at=time.time(),
            requires_network=True, network_allowed=self.allow_network,
            detail="endpoint %s" % self.config.base_url)
        available, detail = self.available()
        if not available:
            health.status = RuntimeState.UNAVAILABLE.value
            health.detail = detail
            health.error = detail
            return health
        if not probe:
            health.status = RuntimeState.READY.value
            health.detail = "not probed (network enabled)"
            health.generations = self._generations
            health.failures = self._failures
            health.timeouts = self._timeouts
            health.cancellations = self._cancellations
            return health
        started = time.perf_counter()
        try:
            models = self.list_models()
        except Exception as exc:
            health.status = RuntimeState.UNAVAILABLE.value
            health.error = redact_text(str(exc))[:400]
            health.detail = "endpoint %s is not reachable" % self.config.base_url
            health.latency_ms = (time.perf_counter() - started) * 1000.0
            self._reachable = False
            self._reachable_detail = health.error
            return health
        health.status = RuntimeState.READY.value
        health.latency_ms = (time.perf_counter() - started) * 1000.0
        health.models_available = len(models)
        health.models_loaded = 0
        health.detail = "%d model(s) served by %s" % (
            len(models), self.config.base_url)
        health.generations = self._generations
        health.failures = self._failures
        health.timeouts = self._timeouts
        health.cancellations = self._cancellations
        self._reachable = True
        self._reachable_detail = health.detail
        return health

    def resources(self) -> Dict[str, Any]:
        return {
            "provider_id": self.config.provider_id,
            "endpoint": self.config.base_url,
            "network_allowed": self.allow_network,
            "timeout_seconds": self.config.timeout,
            "max_response_bytes": self.config.max_response_bytes,
            "generations": self._generations,
            "failures": self._failures,
            "timeouts": self._timeouts,
            "blocked_destinations": self._blocked,
            "api_key_set": bool(self.config.resolved_api_key()),
            "remote": True,
        }

    def status(self, *, probe: bool = True) -> RemoteProviderStatus:
        """``configured`` / ``reachable`` / ``verified`` / ``ready``."""
        status = RemoteProviderStatus(
            provider_id=self.config.provider_id,
            endpoint=self.config.base_url, model=self.config.model,
            configured=True)
        available, detail = self.available()
        if not available:
            status.detail = detail
            status.error = detail
            return status.recompute()
        if probe:
            health = self.health(probe=True)
            status.reachable = health.status == RuntimeState.READY.value
            status.latency_ms = health.latency_ms
            status.detail = health.detail
            status.error = health.error
            status.models = [model.name for model in self.list_models()][:64] \
                if status.reachable else []
        else:
            status.reachable = bool(self._reachable)
            status.detail = self._reachable_detail or "not probed"
        status.verified = any(self._verified_models)
        return status.recompute()

    def mark_verified(self, model_id: str, *, ttl_seconds: float = 0.0) -> None:
        self._verified_models[model_id] = time.time() + max(
            0.0, float(ttl_seconds or 0.0))

    def mark_unverified(self, model_id: str) -> None:
        self._verified_models.pop(model_id, None)

    # -- plumbing --------------------------------------------------------

    def _model_name(self, request: RuntimeRequest) -> str:
        model = (request.model or "").strip()
        if ":" in model and model.split(":", 1)[0] == self.name:
            model = model.split(":", 1)[1]
        return model or self.config.model

    def _payload(self, request: RuntimeRequest, model: str, *,
                 stream: bool) -> bytes:
        messages: List[Dict[str, str]] = []
        system = (request.system or "").strip()
        if not system and request.task and request.task.strip():
            system = request.task.strip()
        if system:
            messages.append({"role": "system", "content": system[:8192]})
        prompt = request.compose_prompt()
        messages.append({"role": "user", "content": prompt})
        body: Dict[str, Any] = {"model": model, "messages": messages,
                                "stream": bool(stream)}
        if request.max_output_tokens is not None:
            body["max_tokens"] = max(1, int(request.max_output_tokens))
        if request.temperature is not None:
            body["temperature"] = float(request.temperature)
        if request.seed is not None:
            body["seed"] = int(request.seed)
        if request.stop:
            body["stop"] = list(request.stop)[:8]
        return json.dumps(body).encode("utf-8")

    def _send(self, method: str, path: str, payload: Optional[bytes],
              request: Optional[RuntimeRequest] = None) -> Dict[str, Any]:
        reason = self._policy_reason()
        if reason:
            return {"ok": False, "blocked": True, "error": reason,
                    "status": 0, "body": b""}
        if not self.allow_network:
            available, detail = self.available()
            return {"ok": False, "blocked": True, "error": detail,
                    "status": 0, "body": b""}
        config = self.config
        url = config.base_url + path
        headers = dict(config.headers)
        headers["Content-Type"] = "application/json"
        api_key = config.resolved_api_key()
        if api_key:
            headers["Authorization"] = "Bearer %s" % api_key
        timeout = config.timeout
        if request is not None and request.timeout:
            timeout = min(config.timeout, max(0.5, float(request.timeout)))
        #: One policy object for the pre-flight check and the send, so a
        #: request can never be admitted under one posture and transmitted
        #: under another.
        policy = config.fetch_policy(timeout=timeout)
        try:
            outcome = hardened_request(method, url, payload=payload,
                                       headers=headers, policy=policy,
                                       audit=self.audit,
                                       allow_redirects=False,
                                       max_bytes=config.max_response_bytes)
        except SSRFError as exc:
            self._blocked += 1
            return {"ok": False, "blocked": True,
                    "error": "destination refused: %s" % exc.reason,
                    "status": 0, "body": b""}
        except Exception as exc:
            return {"ok": False, "blocked": False,
                    "error": redact_text(str(exc))[:400], "status": 0,
                    "body": b""}
        error = ""
        if outcome.blocked:
            self._blocked += 1
            error = "blocked: %s" % redact_text(outcome.blocked_reason)[:300]
        elif not outcome.ok:
            error = redact_text(outcome.error_state or
                                ("HTTP %d" % outcome.status))[:300]
        return {
            "ok": bool(outcome.ok),
            "blocked": bool(outcome.blocked),
            "status": int(outcome.status or 0),
            "body": outcome.body,
            "error": error,
            "timeout": "timed out" in (outcome.error_state or ""),
            "bytes_read": int(outcome.bytes_read or 0),
        }

    def _get_json(self, path: str) -> Any:
        outcome = self._send("GET", path, None)
        if outcome.get("blocked") or not outcome.get("ok"):
            raise BackendUnavailableError(
                "remote provider %s could not answer GET %s: %s"
                % (self.config.provider_id, path,
                   outcome.get("error") or "unknown"))
        try:
            return json.loads(outcome["body"].decode("utf-8", "replace"))
        except ValueError:
            raise BackendUnavailableError(
                "remote provider %s returned a malformed model list"
                % self.config.provider_id)


def _parse_completion(data: Any) -> Tuple[str, str, str, Dict[str, Any]]:
    """Extract ``(text, finish_reason, model, usage)`` from a chat completion."""
    if not isinstance(data, dict):
        return ("", FinishReason.ERROR.value, "", {})
    text = ""
    finish = FinishReason.STOP.value
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                text = str(message.get("content") or "")
            if not text:
                text = str(first.get("text") or "")
            reason = first.get("finish_reason")
            if isinstance(reason, str) and reason:
                finish = _finish(reason)
    if not text:
        text = str(data.get("response") or data.get("text") or "")
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return (text, finish, str(data.get("model") or ""), usage)


def _finish(reason: str) -> str:
    lowered = (reason or "").lower()
    if lowered in ("stop", "end_turn"):
        return FinishReason.STOP.value
    if lowered in ("length", "max_tokens"):
        return FinishReason.LENGTH.value
    if "cancel" in lowered:
        return FinishReason.CANCELLED.value
    if "timeout" in lowered:
        return FinishReason.TIMEOUT.value
    if lowered in ("content_filter", "tool_calls", "function_call"):
        return FinishReason.STOP.value
    return FinishReason.STOP.value


def _same_model(reported: str, wanted: str) -> bool:
    """Tolerate only cosmetic differences (``name`` vs ``name:tag``)."""
    left = (reported or "").strip().lower()
    right = (wanted or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    return left.split(":", 1)[0] == right.split(":", 1)[0] \
        and "/" not in left and "/" not in right


def provider_status(config: RemoteProviderConfig, *,
                    probe: bool = False,
                    allow_network: bool = False) -> RemoteProviderStatus:
    """Report a provider's state without registering a backend."""
    backend = RemoteHttpBackend(config, allow_network=allow_network)
    return backend.status(probe=probe)
