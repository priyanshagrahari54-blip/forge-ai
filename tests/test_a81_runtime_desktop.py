"""A81 — runtime status inside the desktop application.

``forge.desktop_app.backend`` imports no GUI toolkit, so the runtime surface
the desktop window shows is exercised here headlessly.  A test-double runtime
is injected, which also proves the app never depends on a specific backend.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    ExplodingBackend, ModelServingBackend, ScriptedBackend, make_runtime,
)

from forge.desktop_app.backend import BackendError, DesktopBackend  # noqa: E402
from forge.runtime.model_runtime import (  # noqa: E402
    ModelRuntime, NativeBackend, RuntimeConfig, RuntimeRequest,
)


def write_model(root: Path, name: str = "tiny.gguf") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 7, 3) + b"\0" * 32)
    return path


@pytest.fixture()
def backend(tmp_path):
    models = tmp_path / "models"
    write_model(models)
    serving = ModelServingBackend(name="serving", models=["alpha", "beta"])
    runtime = make_runtime(NativeBackend(model_dirs=(str(models),)),
                           serving, default_backend="native")
    app = DesktopBackend(actor="tester", db_path=str(tmp_path / "desktop.db"),
                         runtime=runtime)
    yield app
    app.stop()


# -- injected runtime --------------------------------------------------------


def test_runtime_status_reports_the_injected_runtime(backend):
    status = backend.runtime_status()
    assert status["runtime"]["version"]
    assert status["runtime"]["config"]["allow_network"] is False
    names = sorted(info["name"] for info in status["backends"])
    assert names == ["native", "serving"]
    assert len(status["health"]) == 2
    assert status["resources"]["backends"] == 2


def test_runtime_status_is_redacted(backend):
    backend.runtime.register_backend(ExplodingBackend(name="boom"))
    backend.runtime.generate(RuntimeRequest(prompt="x", backend="boom"))
    payload = str(backend.runtime_status(probe=True))
    assert ExplodingBackend.SECRET not in payload
    assert "[REDACTED]" in payload


def test_runtime_health_verdict_is_honest(backend):
    health = backend.runtime_health(probe=True)
    assert health["ready"] is True
    assert health["ready_backends"] == ["serving"]
    statuses = {item["backend"]: item["status"] for item in health["health"]}
    assert statuses["serving"] == "ready"
    # Discovery-only is degraded, never ready.
    assert statuses["native"] == "degraded"


def test_runtime_health_without_any_capable_backend(tmp_path):
    runtime = ModelRuntime.from_defaults(RuntimeConfig(), load_config=False)
    app = DesktopBackend(runtime=runtime)
    health = app.runtime_health(probe=True)
    assert health["ready"] is False
    assert health["ready_backends"] == []


def test_runtime_models_lists_discovered_artifacts(backend, tmp_path):
    models = backend.runtime_models()
    ids = sorted(model["model_id"] for model in models)
    assert ids == ["native:tiny.gguf", "serving:alpha", "serving:beta"]
    gguf = next(m for m in models if m["name"] == "tiny.gguf")
    assert gguf["format"] == "gguf"
    assert gguf["metadata"]["tensor_count"] == 7
    assert gguf["metadata"]["header_ok"] is True


def test_runtime_models_can_be_scoped_to_one_backend(backend):
    assert [model["backend"] for model in backend.runtime_models("serving")] \
        == ["serving", "serving"]


def test_a_failing_backend_is_reported_not_raised(tmp_path):
    """One broken backend must not hide the others, and must say why."""
    serving = ModelServingBackend(name="serving", models=["alpha"])
    runtime = make_runtime(serving, ExplodingBackend(name="boom"),
                           default_backend="serving")
    app = DesktopBackend(runtime=runtime)

    # Models from the healthy backend are still returned.
    assert [model["model_id"] for model in app.runtime_models()] == [
        "serving:alpha"]

    report = app.runtime_discovery()
    assert report["serving"]["discovered"] == ["serving:alpha"]
    assert report["boom"]["discovered"] == []
    assert "error" in report["boom"]
    assert ExplodingBackend.SECRET not in str(report)
    assert "[REDACTED]" in str(report)


def test_runtime_discovery_can_report_cached_state(tmp_path):
    serving = ModelServingBackend(name="serving", models=["alpha"])
    app = DesktopBackend(runtime=make_runtime(serving,
                                              default_backend="serving"))
    assert app.runtime_discovery()["serving"]["refreshed"] is True
    cached = app.runtime_discovery(refresh=False)
    assert cached["serving"]["refreshed"] is False
    assert cached["serving"]["discovered"] == ["serving:alpha"]


# -- summary used by the polling loop ----------------------------------------


def test_runtime_summary_is_cheap_and_unprobed(backend):
    summary = backend.runtime_summary()
    assert summary["default_backend"] == "native"
    assert summary["allow_network"] is False
    assert sorted(summary["available_backends"]) == ["native", "serving"]
    assert summary["models_known"] == 0  # nothing discovered yet
    assert summary["in_flight"] == 0
    assert summary["cpu_count"] >= 0
    assert summary["memory_total_mb"] >= 0.0
    assert summary["version"]


def test_runtime_summary_reflects_discovery(backend):
    backend.runtime.discover()
    summary = backend.runtime_summary()
    assert summary["models_known"] == 3


def test_runtime_summary_reports_a_broken_runtime():
    class _Broken:
        def backends(self):
            raise RuntimeError("runtime is on fire")

        def status(self, probe=False):
            raise RuntimeError("runtime is on fire")

    app = DesktopBackend(runtime=_Broken())
    with pytest.raises(BackendError) as exc:
        app.runtime_summary()
    assert "runtime is on fire" in str(exc.value)


# -- polling snapshot --------------------------------------------------------


def test_poll_snapshot_includes_runtime_and_survives_a_dead_plane(backend):
    snapshot = backend.poll_snapshot("demo")
    assert snapshot["project_id"] == "demo"
    assert "tasks" in snapshot["errors"]  # the plane was never started
    assert isinstance(snapshot["runtime"], dict)
    assert snapshot["runtime"]["default_backend"] == "native"
    assert "runtime" not in snapshot["errors"]


def test_poll_snapshot_records_a_runtime_failure_without_raising():
    class _Broken:
        def backends(self):
            raise RuntimeError("nope")

    app = DesktopBackend(runtime=_Broken())
    snapshot = app.poll_snapshot("demo")
    assert snapshot["runtime"] is None
    assert "runtime" in snapshot["errors"]


def test_backend_builds_a_runtime_lazily_when_none_is_injected(tmp_path,
                                                             monkeypatch):
    for name in ("FORGE_RUNTIME_BACKEND", "FORGE_RUNTIME_BACKENDS",
                 "FORGE_RUNTIME_ALLOW_NETWORK", "FORGE_RUNTIME_MODEL_DIRS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    app = DesktopBackend()
    status = app.runtime_status()
    assert [info["name"] for info in status["backends"]] == ["native"]
    # The same instance is reused, so counters and discovery persist.
    assert app._runtime() is app.runtime
    assert app.runtime_status()["runtime"]["closed"] is False


def test_desktop_never_enables_network_by_default():
    app = DesktopBackend(runtime=ModelRuntime.from_defaults(
        RuntimeConfig(), load_config=False))
    assert app.runtime_status()["runtime"]["config"]["allow_network"] is False
    health = app.runtime_health(probe=True)
    assert health["ready"] is False
