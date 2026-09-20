"""Quota exhaustion and failover across the operator's own servers.

The requirement: "one provider's limit is spent, use the next one". These tests
pin the three layers that make that true — recognising an exhausted provider,
rotating across the servers that serve the same model, and skipping a spent
provider (until its cooldown passes) so requests fail over instead of retrying
a wall. The HTTP servers are real; nothing here fakes transport.
"""
from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forge.models.config import FabricConfig, group_local_endpoints
from forge.models.endpoints import LocalEndpoint
from forge.models.errors import ProviderExhaustedError
from forge.models.fabric import ModelFabric
from forge.models.local_openai import LocalOpenAIProvider
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.quota import (
    DEFAULT_COOLDOWN_SECONDS,
    ExhaustionTracker,
    classify_exhaustion,
)
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest


# -- classification ----------------------------------------------------------

def test_a_spent_provider_is_recognised_by_status_and_by_wording():
    typed = ProviderExhaustedError("quota spent", retry_after=30.0, status=429)
    assert classify_exhaustion(typed).exhausted is True
    assert classify_exhaustion(typed).retry_after == 30.0

    http_429 = urllib.error.HTTPError(
        "http://x/v1/chat/completions", 429, "Too Many Requests", {}, None)
    signal = classify_exhaustion(http_429)
    assert signal.exhausted is True and signal.status == 429

    for message in ("insufficient_quota", "rate limit exceeded",
                    "You exceeded your current quota",
                    "Error 429: too many requests",
                    "no credits remaining for this account"):
        assert classify_exhaustion(RuntimeError(message)).exhausted is True, message


def test_a_broken_endpoint_is_not_mistaken_for_a_full_one():
    """A 500 or a connection error must not start a cooldown: the provider is
    faulty, not spent, and silently skipping it would hide a real outage."""
    for message in ("connection refused", "HTTP 500: internal error",
                    "model not found", "invalid api key"):
        assert classify_exhaustion(RuntimeError(message)).exhausted is False, message
    http_500 = urllib.error.HTTPError("http://x", 500, "Server Error", {}, None)
    assert classify_exhaustion(http_500).exhausted is False


def test_exhaustion_tracker_cooldown_expires_on_its_own():
    now = [1000.0]
    tracker = ExhaustionTracker(cooldown_seconds=60.0, clock=lambda: now[0])
    signal = classify_exhaustion(ProviderExhaustedError("spent", retry_after=30.0))

    applied = tracker.record("local-openai", "m", signal)
    assert applied == 30.0
    assert tracker.is_exhausted("local-openai", "m") is True
    assert tracker.remaining("local-openai", "m") == pytest.approx(30.0)

    now[0] += 31.0
    # Not a blacklist: the provider is tried again once its cooldown passes.
    assert tracker.is_exhausted("local-openai", "m") is False
    assert tracker.snapshot()["exhausted_count"] == 0


def test_a_stated_retry_delay_is_honoured_but_capped():
    tracker = ExhaustionTracker(cooldown_seconds=60.0, max_cooldown_seconds=900.0,
                                clock=lambda: 0.0)
    huge = classify_exhaustion(ProviderExhaustedError("spent", retry_after=86400.0))
    assert tracker.cooldown_for(huge) == 900.0
    silent = classify_exhaustion(ProviderExhaustedError("spent"))
    assert tracker.cooldown_for(silent) == 60.0 == DEFAULT_COOLDOWN_SECONDS


# -- rotation inside one provider, over real HTTP ------------------------------

class _SpentEndpoint:
    """A real HTTP server that answers every request with a quota refusal."""

    def __init__(self, *, status: int = 429, retry_after: str = "1",
                 body: str = '{"error": {"message": "insufficient_quota"}}'):
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                outer.requests += 1
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", retry_after)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_POST = _respond
            do_GET = _respond

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _ServingEndpoint:
    """A real HTTP server that serves a model and counts requests."""

    def __init__(self, text: str = "real answer"):
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                payload = json.dumps({
                    "model": "test-model",
                    "choices": [{"message": {"content": text},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                outer.requests += 1
                payload = json.dumps({"data": [{"id": "test-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_spent_server_is_rotated_to_the_next_one_serving_the_same_model():
    spent, alive = _SpentEndpoint(retry_after="1"), _ServingEndpoint("from B")
    try:
        provider = LocalOpenAIProvider(
            model="test-model",
            endpoints=(LocalEndpoint(url=spent.url, label="server-a"),
                       LocalEndpoint(url=alive.url, label="server-b")),
        )
        result = provider.generate("hello", max_output_tokens=16)

        assert result.text == "from B"
        assert result.metadata["endpoint_used"] == alive.url
        assert result.metadata["endpoint_label"] == "server-b"
        assert result.metadata["endpoints_skipped_exhausted"] == ["server-a"]

        # The spent server is remembered, so the next call does not even ask it.
        before = spent.requests
        second = provider.generate("again", max_output_tokens=16)
        assert second.text == "from B"
        assert spent.requests == before, "a spent endpoint must be skipped, not retried"
    finally:
        spent.close()
        alive.close()


def test_when_every_server_is_spent_the_provider_says_so_with_a_delay():
    first = _SpentEndpoint(retry_after="7")
    second = _SpentEndpoint(retry_after="")
    try:
        provider = LocalOpenAIProvider(
            model="test-model",
            endpoints=(LocalEndpoint(url=first.url, label="a"),
                       LocalEndpoint(url=second.url, label="b")),
        )
        with pytest.raises(ProviderExhaustedError) as caught:
            provider.generate("hello")
        # Both were tried within this single request before giving up.
        assert first.requests == 1 and second.requests == 1
        assert "exhausted" in str(caught.value)
    finally:
        first.close()
        second.close()


def test_a_faulty_server_is_not_marked_spent_and_its_error_is_raised():
    broken = _SpentEndpoint(status=500, body='{"error": "boom"}')
    try:
        provider = LocalOpenAIProvider(
            model="test-model", endpoints=(LocalEndpoint(url=broken.url),))
        with pytest.raises(RuntimeError) as caught:
            provider.generate("hello")
        assert not isinstance(caught.value, ProviderExhaustedError)
        # A 500 is a defect, not a spent quota: no cooldown was started.
        assert provider.exhaustion.snapshot()["exhausted_count"] == 0
    finally:
        broken.close()


def test_a_probe_succeeds_when_any_configured_server_answers():
    spent, alive = _SpentEndpoint(), _ServingEndpoint()
    try:
        provider = LocalOpenAIProvider(
            model="test-model",
            endpoints=(LocalEndpoint(url=spent.url), LocalEndpoint(url=alive.url)),
        )
        # A spent endpoint still lists the model; one answer is enough evidence.
        assert provider.list_models() == ["test-model"]
    finally:
        spent.close()
        alive.close()


# -- configuration: several of the operator's own servers ----------------------

def test_numbered_environment_variables_describe_several_servers():
    env = {
        "FORGE_LOCAL_MODEL_URL": "http://box-a:8080",
        "FORGE_LOCAL_MODEL_NAME": "small-model.gguf",
        "FORGE_LOCAL_MODEL_URL_2": "http://box-b:8080",
        "FORGE_LOCAL_MODEL_NAME_2": "big-model.gguf",
        "FORGE_LOCAL_MODEL_TIER_2": "50",
        "FORGE_LOCAL_MODEL_LABEL_2": "gpu-box",
    }
    config = FabricConfig.from_dict({}, env=env)

    assert [e.url for e in config.local_openai_endpoints] == [
        "http://box-a:8080", "http://box-b:8080"]
    groups = group_local_endpoints(config.local_openai_endpoints)
    assert [g[0] for g in groups] == ["small-model.gguf", "big-model.gguf"]
    assert groups[1][2] == 50          # tier travels with the model
    assert config.local_openai_endpoints[1].label == "gpu-box"


def test_the_same_model_on_two_servers_is_one_group_so_it_can_rotate():
    env = {
        "FORGE_LOCAL_MODEL_URL": "http://box-a:8080",
        "FORGE_LOCAL_MODEL_NAME": "m.gguf",
        "FORGE_LOCAL_MODEL_URL_2": "http://box-b:8080",
        "FORGE_LOCAL_MODEL_NAME_2": "m.gguf",
    }
    config = FabricConfig.from_dict({}, env=env)
    groups = group_local_endpoints(config.local_openai_endpoints)
    assert len(groups) == 1
    assert len(groups[0][3]) == 2, "same model on two servers must pool"


def test_a_json_endpoint_list_is_accepted_and_deduped():
    env = {
        "FORGE_LOCAL_MODEL_URL": "http://box-a:8080",
        "FORGE_LOCAL_MODEL_NAME": "m.gguf",
        "FORGE_MODEL_ENDPOINTS": json.dumps([
            {"url": "http://box-b:8080", "model": "m.gguf", "tier": 10},
            {"url": "http://box-a:8080", "model": "m.gguf"},   # duplicate
            {"url": "", "model": "ignored.gguf"},
        ]),
    }
    config = FabricConfig.from_dict({}, env=env)
    urls = [e.url for e in config.local_openai_endpoints]
    assert urls == ["http://box-a:8080", "http://box-b:8080"]


def test_a_single_endpoint_configuration_is_completely_unchanged():
    """The pre-multi-server configuration must behave exactly as before."""
    env = {"FORGE_LOCAL_MODEL_URL": "http://only:8080",
           "FORGE_LOCAL_MODEL_NAME": "only.gguf"}
    config = FabricConfig.from_dict({}, env=env)
    fabric = ModelFabric.from_defaults(config)

    local_models = [m for m in fabric.registry
                    if m.provider.startswith("local-openai")]
    assert [m.name for m in local_models] == ["only.gguf"]
    model = fabric.registry.get("only.gguf")
    assert model.provider == "local-openai"
    assert model.metadata["tier"] == 0
    assert [e["url"] for e in model.metadata["endpoints"]] == ["http://only:8080"]


def test_two_models_get_two_providers_and_their_own_tiers():
    env = {
        "FORGE_LOCAL_MODEL_URL": "http://small:8080",
        "FORGE_LOCAL_MODEL_NAME": "small.gguf",
        "FORGE_LOCAL_MODEL_URL_2": "http://big:8080",
        "FORGE_LOCAL_MODEL_NAME_2": "big.gguf",
        "FORGE_LOCAL_MODEL_TIER_2": "80",
    }
    fabric = ModelFabric.from_defaults(FabricConfig.from_dict({}, env=env))

    providers = {m.name: m.provider for m in fabric.registry
                 if m.provider.startswith("local-openai")}
    assert providers == {"small.gguf": "local-openai",
                         "big.gguf": "local-openai-2"}
    assert fabric.registry.get("big.gguf").metadata["tier"] == 80


# -- routing: a spent provider is skipped, power is preferred -------------------

class _Provider:
    def __init__(self, name, *, error=None, text="ok"):
        self.name = name
        self.error = error
        self.text = text
        self.calls = 0

    def generate(self, prompt, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ModelResult(self.text, self.name)


def _fabric_for(models, providers, *, config=None):
    return ModelFabric(
        registry=ModelRegistry(models),
        providers=ProviderRegistry(providers),
        config=config,
    )


def test_a_spent_provider_is_skipped_by_the_next_request():
    spent = _Provider("spent", error=ProviderExhaustedError(
        "insufficient_quota", retry_after=120.0, status=429))
    alive = _Provider("alive", text="served by the second provider")
    fabric = _fabric_for(
        [Model(name="m1", provider="spent", capabilities=("coding",)),
         Model(name="m2", provider="alive", capabilities=("coding",))],
        {"spent": spent, "alive": alive},
        config=FabricConfig(provider_cooldown_seconds=300.0),
    )
    fabric.exhaustion.clock = lambda: fabric._test_now[0]
    fabric._test_now = [0.0]

    first = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert first.success and first.text == "served by the second provider"
    assert spent.calls == 1 and alive.calls == 1
    assert fabric.quota_snapshot()["exhausted_count"] == 1
    assert fabric.quota_snapshot()["exhausted"][0]["provider"] == "spent"

    # Second request: the spent provider is not even contacted.
    second = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert second.success
    assert spent.calls == 1, "an exhausted provider must be skipped, not retried"
    assert alive.calls == 2

    # Once the cooldown passes the provider is eligible again — not
    # blacklisted. (Which model the router then prefers is a separate
    # decision, so this asserts eligibility, not the ordering.)
    fabric._test_now[0] += 400.0
    assert fabric.quota_snapshot()["exhausted_count"] == 0
    decision = fabric.route(ModelRequest(prompt="p", capability="coding"))
    assert "m1" in decision.candidates, (
        "an expired cooldown must make the spent provider eligible again")


def test_when_every_provider_is_spent_the_failure_says_which_and_for_how_long():
    a = _Provider("a", error=ProviderExhaustedError("insufficient_quota",
                                                    retry_after=45.0))
    b = _Provider("b", error=ProviderExhaustedError("rate limit exceeded"))
    fabric = _fabric_for(
        [Model(name="m1", provider="a", capabilities=("coding",)),
         Model(name="m2", provider="b", capabilities=("coding",))],
        {"a": a, "b": b},
        config=FabricConfig(provider_cooldown_seconds=60.0),
    )
    fabric.exhaustion.clock = lambda: 0.0

    response = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert response.success is False
    spent = response.metadata["providers_exhausted"]
    assert {entry["provider"] for entry in spent} == {"a", "b"}
    assert all(entry["seconds_remaining"] > 0 for entry in spent)


def test_a_non_quota_failure_does_not_start_a_cooldown():
    broken = _Provider("broken", error=RuntimeError("connection refused"))
    alive = _Provider("alive", text="ok")
    fabric = _fabric_for(
        [Model(name="m1", provider="broken", capabilities=("coding",)),
         Model(name="m2", provider="alive", capabilities=("coding",))],
        {"broken": broken, "alive": alive},
    )
    response = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert response.success and response.text == "ok"
    assert fabric.quota_snapshot()["exhausted_count"] == 0
    assert broken.calls == 1


def test_a_recovered_provider_clears_its_exhaustion():
    flaky = _Provider("flaky", error=ProviderExhaustedError("rate limit"))
    fabric = _fabric_for(
        [Model(name="m1", provider="flaky", capabilities=("coding",))],
        {"flaky": flaky}, config=FabricConfig(provider_cooldown_seconds=0.0))
    fabric.generate(ModelRequest(prompt="p", capability="coding"))
    flaky.error = None
    flaky.text = "back online"
    response = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert response.success and response.text == "back online"
    assert fabric.quota_snapshot()["exhausted_count"] == 0


def test_routing_prefers_the_more_powerful_model_that_can_do_the_job():
    def model(name, tier):
        return Model(name=name, provider=name, capabilities=("coding",),
                     metadata={"tier": tier} if tier is not None else {})

    providers = {name: _Provider(name, text=f"answered by {name}")
                 for name in ("small", "big", "undeclared")}
    fabric = _fabric_for(
        [model("small", 10), model("big", 90), model("undeclared", None)],
        providers,
    )
    response = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert response.text == "answered by big"

    # A weaker-only fleet still works, and undeclared tiers keep old ordering.
    fabric2 = _fabric_for([model("small", 10), model("undeclared", None)],
                          providers)
    assert fabric2.generate(
        ModelRequest(prompt="p", capability="coding")).text == "answered by small"


def test_a_model_that_cannot_do_the_job_is_never_chosen_for_power():
    def model(name, tier, capabilities):
        return Model(name=name, provider=name, capabilities=capabilities,
                     metadata={"tier": tier})

    providers = {"big": _Provider("big", text="big"),
                 "coder": _Provider("coder", text="coder")}
    fabric = _fabric_for(
        [model("big", 99, ("reasoning",)), model("coder", 1, ("coding",))],
        providers)

    response = fabric.generate(ModelRequest(prompt="p", capability="coding"))
    assert response.text == "coder", "tier must never override capability"


class _ThreeEightHTTPError(Exception):
    """A stand-in for Python 3.8's ``urllib.error.HTTPError``.

    On that interpreter an HTTPError delegates unknown attributes to the stdlib
    file wrapper, whose ``__getattr__`` raises ``KeyError('file')`` instead of
    ``AttributeError``. Classification read ``retry_after`` and ``headers`` off
    such an exception, so a real 429 crashed the failover path on Python 3.8
    (observed in CI as ``KeyError: 'file'``). The class below reproduces the
    delegate exactly, on every interpreter.
    """

    def __init__(self, *, code: int = 429, headers=None) -> None:
        self.code = code
        self.headers = headers if headers is not None else {"Retry-After": "12"}

    def __getattr__(self, name: str):
        raise KeyError("file")

    def __str__(self) -> str:
        return f"HTTP Error {self.code}: {self.reason_phrase}"

    @property
    def reason_phrase(self) -> str:
        return {429: "Too Many Requests", 500: "Internal Server Error"}.get(
            self.code, "Error")


def test_classification_survives_an_exception_whose_attribute_lookup_raises():
    signal = classify_exhaustion(_ThreeEightHTTPError())

    assert signal.exhausted is True
    assert signal.status == 429
    # The stated delay is still read from the headers once attribute lookup is
    # guarded — the fix must not turn a known delay into "unknown".
    assert signal.retry_after == 12.0


def test_a_hostile_exception_is_never_mistaken_for_an_exhausted_provider():
    """Guarding the lookup must not make every failure look like exhaustion."""
    signal = classify_exhaustion(_ThreeEightHTTPError(code=500, headers={}))

    assert signal.exhausted is False
    assert signal.status == 500
