"""Live Ollama autonomous-coding end-to-end test (opt-in).

Skipped unless the operator explicitly opts in AND an Ollama endpoint is
reachable, so ordinary CI stays green offline.

    FORGE_LIVE_OLLAMA=1        (primary opt-in for this heavier E2E)
    FORGE_LIVE_MODEL_TESTS=1   (existing blanket opt-in also enables it)

Optional tuning:

    FORGE_TEST_OLLAMA_MODEL    target a specific pulled model (default: first
                               model reported by the endpoint's /api/tags)

The test drives the real production path — Supervisor -> Model Fabric -> Router
-> Ollama -> CoderAgent -> permissioned writes -> pytest -> review/security ->
benchmark -> explicit commit — for a tiny deterministic requirement, and then
verifies the actual repository behavior. No model response is faked, no
caller-supplied changes and no modifier function are used.
"""
import importlib.util
import os
import socket
import subprocess
import sys
from urllib.parse import urlparse

import pytest

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import OllamaProvider, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


REQUIREMENT = (
    "Add a function named celsius_to_fahrenheit to app.py that converts "
    "Celsius to Fahrenheit using the formula value * 9 / 5 + 32, and add a "
    "test in tests/ that checks 0 converts to 32 and 100 converts to 212."
)


def _enabled() -> bool:
    ollama_flag = os.getenv("FORGE_LIVE_OLLAMA", "").lower() in {"1", "true", "yes", "on"}
    blanket = os.getenv("FORGE_LIVE_MODEL_TESTS", "").lower() in {"1", "true", "yes", "on"}
    return ollama_flag or blanket


def _endpoint() -> tuple[str, int]:
    url = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 11434


def _reachable() -> bool:
    host, port = _endpoint()
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _skip_reason() -> str | None:
    if not _enabled():
        return "FORGE_LIVE_OLLAMA (or FORGE_LIVE_MODEL_TESTS) is not enabled"
    if not _reachable():
        return "no reachable Ollama endpoint"
    return None


def _resolve_model(provider: OllamaProvider) -> str:
    requested = os.getenv("FORGE_TEST_OLLAMA_MODEL")
    if requested:
        return requested
    models = provider.list_models()
    if not models:
        pytest.skip("no models pulled on the Ollama endpoint")
    return models[0]


def _module_from_file(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ollama_autonomous_coding_end_to_end(tmp_path):
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    # 1-2. A tiny working codebase in an isolated repository.
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    # 3-4. Route through the Model Fabric against the live Ollama endpoint.
    base_url = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL")
    probe = OllamaProvider(url=base_url)
    model_name = _resolve_model(probe)
    provider = OllamaProvider(model=model_name, url=base_url)
    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(
                name=f"ollama/{model_name}",
                provider="ollama-live",
                capabilities=("coding", "debugging", "structured_output"),
                context_window=8192,
                free=True,
                local=True,
            ),
        ]),
        providers=ProviderRegistry({"ollama-live": provider}),
    )

    # 5-10. Full supervised transaction: model-generated change, permissioned
    # writes, tests, review/security, benchmark, explicit commit.
    outcome = Supervisor("live-ollama", tmp_path).run(
        REQUIREMENT, approved=True, fabric=fabric, max_debug_retries=3,
    )

    # 11. Verify the resulting behavior, not just a green gate.
    assert outcome["accepted"] is True, (
        f"autonomous run was not accepted: {outcome.get('failure_reason') or outcome.get('error')}"
    )
    app_source = (tmp_path / "app.py").read_text()
    assert "celsius_to_fahrenheit" in app_source, "generated app.py is missing the requested function"

    sys.path.insert(0, str(tmp_path))
    try:
        app = _module_from_file(tmp_path / "app.py", "live_app")
    finally:
        sys.path.remove(str(tmp_path))
    assert callable(getattr(app, "celsius_to_fahrenheit", None))
    assert app.celsius_to_fahrenheit(0) == 32
    assert app.celsius_to_fahrenheit(100) == 212

    # 12. The touched files are exactly the validated application files.
    files = outcome.get("files", [])
    assert "app.py" in files
    assert any(path.startswith("tests/") and path.endswith(".py") for path in files)

    # 13. A real commit happened with only the touched files.
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True,
    ).stdout.splitlines()
    assert set(committed) == set(files)
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True).stdout
    assert ".forge" not in status

    # Routing + provider outcomes were recorded by the fabric.
    assert fabric.telemetry.count("route") >= 2
    assert fabric.telemetry.count("response") >= 1
    assert any(event["success"] for event in fabric.router.history)
