"""Regression tests for the production-hardening defects.

Each test pins one bug that was found during the production audit so it
cannot silently return:

1. the runtime monitor marking a working model UNAVAILABLE because its
   provider exposes no model-list probe, which then made every autonomous
   task fail the readiness pre-flight gate;
2. the monitor service crashing on duck-typed registries (missing
   ``health``/``metadata``) and reporting the crash as a model outage;
3. the app lifespan not being re-entrant (nested ``with client`` collapsed
   ``plane.runtime_monitor`` to ``None`` and teardown raised);
4. the 1000-slot fleet dropping its last specializations (1,000/26 = 38.5);
5. the lease guard publishing a result after ownership loss;
6. the container-execution gateway failing to construct (``heartbeat_ttl``);
7. ``GET /capabilities`` and ``GET /provider-links`` being unreachable
   because ``request: Any`` became a required query parameter;
8. the deterministic "check status" phrase classifying as a review;
9. AI City being unreachable from the cockpit navigation while the voice
   surfaces each carried their own playback implementation, the cockpit
   bypassing confirm-before-execute, and the city page persisting a bearer
   token in ``localStorage``.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from forge.agents.frontier_fleet import SPECIALIZATIONS, build_frontier_fleet
from forge.capabilities.http import install_capability_route
from forge.capabilities.runtime import provider_health_from_fabric
from forge.conversation.model_reply import ModelConversationalist
from forge.core.lease_guard import LeaseGuard, LeaseLostError
from forge.core.lease_monitor import LeaseMonitor
from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore
from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider
from forge.models.provider_links_http import install_provider_links_route
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.models.runtime_monitor_service import RuntimeMonitorService
from forge.models.runtime_verification import RuntimeProbeResult, apply_probe_result
from forge.voice.base import VoiceCommand, VoiceInterface


class _ListlessProvider:
    """A real provider adapter with no model-list endpoint (custom HTTP)."""

    name = "custom"

    def generate(self, prompt, **kwargs):
        return "ok"


class _MinimalRegistry:
    """Duck-typed registry used by adapters: snapshot + get, nothing else."""

    def __init__(self, name="model-a", provider="custom"):
        self._name, self._provider = name, provider

    def snapshot(self):
        return [{"name": self._name, "provider": self._provider}]

    def get(self, name):
        return type("Model", (), {"name": name, "provider": self._provider,
                                  "fallback": False, "available": True})()


class _MinimalFabric:
    def __init__(self, registry=None):
        self.registry = registry or _MinimalRegistry()
        self.providers = {"custom": _ListlessProvider()}


def test_listless_provider_is_unverified_not_unavailable():
    """Inconclusive verification must never be recorded as an outage."""
    provider = _ListlessProvider()
    fabric = ModelFabric(registry=ModelRegistry([
        Model(name="m/a34", provider="custom", capabilities=("coding",))]),
        providers=_MinimalProviderRegistry(provider))
    service = RuntimeMonitorService(fabric, state_path=Path(tempfile.mkdtemp()) / "r.json")
    service.tick(force=True, now=100.0)
    model = fabric.registry.get("m/a34")
    assert model.available is True
    assert model.metadata.get("runtime_verified") is not True
    runtime = service.registry.get("custom:m/a34")
    assert runtime.state != "UNAVAILABLE"
    assert "inconclusive" in runtime.last_reason
    # The task pipeline's pre-flight gate therefore still sees a real model.
    from forge.models.readiness import fabric_has_real_model
    assert fabric_has_real_model(fabric) is True


class _MinimalProviderRegistry:
    """ProviderRegistry-compatible stand-in for one provider."""

    def __init__(self, provider):
        self._providers = {provider.name: provider}

    def get(self, name):
        return self._providers.get(name)

    def names(self):
        return sorted(self._providers)

    def has(self, name):
        return name in self._providers

    def items(self):
        return [(name, self._providers[name]) for name in self.names()]


def test_inconclusive_probe_does_not_flip_availability():
    model = Model(name="m1", provider="custom", capabilities=("coding",))
    registry = ModelRegistry([model])
    result = RuntimeProbeResult(model_id="m1", ok=False, status="unverified",
                                reason="no probe", conclusive=False)
    assert apply_probe_result(registry, result) is model
    assert model.available is True
    assert model.metadata.get("runtime_verified") is not True


def test_probe_result_apply_tolerates_minimal_models():
    """A registry model without health/metadata must not crash the monitor."""
    registry = _MinimalRegistry()
    result = RuntimeProbeResult(model_id="model-a", ok=True, status="healthy")
    updated = apply_probe_result(registry, result)
    assert updated is not None and updated.available is True


def test_monitor_service_survives_duck_typed_registry():
    fabric = _MinimalFabric()
    service = RuntimeMonitorService(fabric, state_path=Path(tempfile.mkdtemp()) / "r.json")
    snapshot = service.tick(force=True, now=100.0)
    assert snapshot["live"] == 0  # no list probe -> unverified, never "live"
    assert snapshot["counts"]["UNAVAILABLE"] == 0


def test_configured_runtime_registry_accepts_composite_keys():
    registry = ConfiguredRuntimeRegistry()
    runtime = ConfiguredRuntime(provider="fake", model_id="model-live")
    registry.register(runtime)
    assert registry.get("fake:model-live") is runtime
    assert registry.maybe_get("fake:model-live") is runtime
    assert registry.get("fake", "model-live") is runtime


def test_provider_health_adapter_accepts_provider_registry():
    provider = _ListlessProvider()
    fabric = _MinimalFabric()
    fabric.providers = _MinimalProviderRegistry(provider)
    health = provider_health_from_fabric(fabric)
    assert "custom" in health
    assert health["custom"]["verified"] is False


def test_app_lifespan_is_reentrant(tmp_path):
    """Nested/repeated app lifespans must not null each other's services."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from helpers_a34 import make_client, make_plane, make_repo  # noqa: WPS433

    plane = make_plane(tmp_path, start=False)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        with client:  # second, overlapping entry (previously raised)
            assert plane.runtime_monitor is not None
            assert plane.worker_heartbeat_service is not None
    # Teardown never leaves a *stopped* service attached to the plane: a
    # caller would otherwise schedule work onto a dead monitor/heartbeat.
    for attribute in ("runtime_monitor", "worker_heartbeat_service"):
        service = getattr(plane, attribute, None)
        assert service is None or service.running, attribute


def test_frontier_fleet_covers_every_specialization():
    fabric = _RecordingFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    assert len(registry) == 1000
    roles = {role for _name, role, _caps in SPECIALIZATIONS}
    assert set(registry.roles()) == roles
    assert len(registry.roles()) == len(SPECIALIZATIONS)
    # Every slot is executable through the shared fabric.
    slot = registry.get(sorted(registry.names())[0])
    assert callable(slot.executor.execute)


class _RecordingFabric:
    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        return type("Response", (), {
            "success": True, "text": "ok", "model": "fake-model",
            "provider": "fake", "metadata": {},
        })()


def _queue(tmp_path):
    store = TaskStore(Path(tmp_path) / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "long running work")
    claimed = queue.start_next()
    assert claimed is not None
    return queue, claimed


def test_lease_guard_verifies_ownership_at_publish_time(tmp_path):
    """A worker that lost its lease must refuse to publish its result."""
    queue, claimed = _queue(tmp_path)
    monitor = LeaseMonitor(queue, task_id=claimed.id,
                           lease_id=claimed.lease_id,
                           heartbeat_interval=0.01, stale_after=0.05)
    recovered = queue.recover_stale_running(0.001)
    assert recovered and recovered[0].status == TaskStatus.RECOVERY
    try:
        LeaseGuard(monitor, lambda: "must-not-publish").run()
    except LeaseLostError:
        pass
    else:  # pragma: no cover - the refusal is the whole point
        raise AssertionError("LeaseGuard published after ownership loss")


def test_leaseless_worker_still_publishes(tmp_path):
    queue, claimed = _queue(tmp_path)
    monitor = LeaseMonitor(queue, task_id=claimed.id,
                           lease_id=claimed.lease_id,
                           heartbeat_interval=0.01, stale_after=0.05)
    assert LeaseGuard(monitor, lambda: "ok").run() == "ok"
    assert not monitor.lost


def test_execution_gateway_accepts_heartbeat_ttl():
    from forge.workers.execution_gateway import ExecutionGateway
    from forge.workers.registry import WorkerRegistry

    registry = WorkerRegistry(heartbeat_ttl=5.0)
    registry.register(name="w", capabilities=("python",), cpu_threads=2,
                      ram_mb=2048, platform="linux")
    gateway = ExecutionGateway(registry, heartbeat_ttl=30.0)
    assert registry.heartbeat_ttl == 30.0
    assert gateway.admission.registry is registry


def test_capability_and_provider_link_routes_are_reachable():
    """``request: Any`` used to become a required query parameter."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    class _Authorizer:
        def require(self, principal, operation):
            assert operation == "models.status"
            return principal

    install_capability_route(
        app, require=lambda request, op: _Authorizer().require(None, op),
        server_getter=lambda request: type("S", (), {"fabric": None})())
    install_provider_links_route(
        app, require=lambda request, op: _Authorizer().require(None, op))
    with TestClient(app) as client:
        capabilities = client.get("/api/v1/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        links = client.get("/api/v1/provider-links")
        assert links.status_code == 200, links.text
        assert links.json()["providers"]


def test_model_conversation_requires_a_verified_live_model():
    """No runtime-verified model means no model answer (never a fake one)."""
    fallback = Model(name="placeholder", provider="local", capabilities=("coding",))
    fabric = ModelFabric(registry=ModelRegistry([fallback]),
                         providers=_MinimalProviderRegistry(_ListlessProvider()))
    conversationalist = ModelConversationalist(fabric)
    assert conversationalist.available() is False
    assert conversationalist.reply("hello") is None
    assert "runtime-verified" in conversationalist.unavailable_reason()


def test_model_conversation_uses_a_verified_model():
    provider = _RecordingProvider("Forge is running the CSV export task.")
    model = Model(name="live-model", provider="custom", capabilities=("reasoning",))
    model.metadata["runtime_verified"] = True
    fabric = ModelFabric(registry=ModelRegistry([model]),
                         providers=_MinimalProviderRegistry(provider))
    reply = ModelConversationalist(fabric).reply("what are you doing?")
    assert reply is not None
    assert reply.text == "Forge is running the CSV export task."
    assert reply.model == "live-model"
    assert provider.prompts and "You are Forge" in provider.prompts[0]


class _RecordingProvider:
    name = "custom"

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        from forge.models.provider import ModelResult
        return ModelResult(self.response, "live-model")


def test_check_status_is_a_status_intent_not_a_review():
    voice = VoiceInterface()
    intent = voice.parse(VoiceCommand("check status"))
    assert intent.name == "status"
    assert voice.parse(VoiceCommand("Forge, check status")).name == "status"


def test_wake_prefixed_website_command_is_recognized():
    voice = VoiceInterface()
    assert voice.parse(VoiceCommand("Forge, update the website.")).name == \
        "update_website"
    assert voice.parse(VoiceCommand("update the website")).name == \
        "update_website"


# -- cockpit surfaces ----------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "forge" / "cockpit" / "web"


def _web(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_ai_city_is_reachable_from_the_main_navigation():
    html = _web("index.html")
    js = _web("app.js")
    assert 'href="#/city"' in html
    assert 'data-route="city"' in html
    assert 'id="tpl-city"' in html
    assert 'id="city-frame"' in html and 'src="/city.html"' in html
    assert re.search(r"\bcity:\s*\{\s*render:\s*renderCityView", js)


def test_city_view_only_hosts_real_backend_state():
    city = _web("city.html")
    assert "REAL BACKEND EVENTS" in city
    assert "NO SYNTHETIC PROGRESS" in city
    assert "/tasks?limit=50" in city
    assert "events/stream" in city
    # the city never animates itself: no timer of its own, no fake stages
    assert "setInterval" not in city


def test_city_page_keeps_the_bearer_token_out_of_persistent_storage():
    city = _web("city.html")
    assert "sessionStorage.setItem('forge.city.token'" in city
    assert "localStorage.setItem('forge.city.token'" not in city


def test_both_voice_surfaces_use_one_playback_implementation():
    index = _web("index.html")
    home = _web("forge-home.html")
    handsfree = _web("handsfree.js")
    assert index.index("/voice-playback.js") < index.index("/handsfree.js")
    assert 'src="/voice-playback.js"' in home
    assert 'id="enable-voice"' in home
    assert "ForgeVoicePlayback" in home
    assert "speechSynthesis.speak(" not in handsfree
    assert "window.ForgeVoicePlayback" in handsfree


def test_voice_commands_still_ask_before_executing():
    handsfree = _web("handsfree.js")
    home = _web("forge-home.html")
    assert "body: {text, confirm: true}" in handsfree
    assert "confirm:true" in home


