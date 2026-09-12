"""Shared fixtures for the A81 desktop-server link test suites."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from forge.api.app import create_app  # noqa: E402
from forge.client.config import ClientConfig  # noqa: E402
from forge.client.transport import LinkTransport  # noqa: E402
from forge.control import ControlConfig, ControlPlane  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.models.provider import ModelResult, ProviderRegistry  # noqa: E402
from forge.models.registry import Model, ModelRegistry  # noqa: E402

from helpers_a34 import CSV_PAYLOAD, make_repo  # noqa: E402  (re-exported)

__all__ = ["CSV_PAYLOAD", "ScriptedProvider", "BlockingProvider",
           "make_link_env", "make_client_config", "make_client_for",
           "make_repo", "TestClient", "wait_until"]


class ScriptedProvider:
    """Deterministic provider: scripted coder payload, approving review."""

    name = "scripted"

    def __init__(self, coder_payload: str = CSV_PAYLOAD) -> None:
        self.coder_payload = coder_payload

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            return ModelResult(
                '{"findings": [], "verdict": "APPROVE"}', self.name)
        return ModelResult(self.coder_payload, self.name)


class BlockingProvider(ScriptedProvider):
    """Scripted provider that blocks inside generate() until released."""

    name = "blocking"

    def __init__(self, coder_payload: str = CSV_PAYLOAD) -> None:
        import threading
        super().__init__(coder_payload)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, prompt, **kwargs):
        if "Review the following change set" not in prompt:
            self.entered.set()
            assert self.release.wait(timeout=60), "provider wait timed out"
        return super().generate(prompt, **kwargs)


def make_fabric(provider) -> ModelFabric:
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a81", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


class LinkEnv:
    """Server + signed HTTP adapter for one deterministic test setup."""

    def __init__(self, tmp: Path, provider=None, *, project_id: str = "demo",
                 approval_timeout: float = 30.0, start: bool = True) -> None:
        self.tmp = tmp
        root = tmp / project_id
        root.mkdir(parents=True, exist_ok=True)
        self.plane = ControlPlane(ControlConfig(
            db_path=str(tmp / "server.db"), projects={project_id: str(root)},
            fabric=make_fabric(provider or ScriptedProvider()),
            approval_timeout=approval_timeout,
            approval_token_ttl=30.0))
        if start:
            self.plane.start()
        self.app = create_app(self.plane)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.project_id = project_id
        self.service = self.app.state.link
        self.requests: list[dict[str, Any]] = []

    # -- repo ----------------------------------------------------------------

    def make_repo(self) -> Path:
        make_repo(Path(self.plane.projects[self.project_id].root))
        return Path(self.plane.projects[self.project_id].root)

    # -- client management ----------------------------------------------------

    def register(self, client_id: str = "g560", **kwargs) -> str:
        return self.service.register_client(
            client_id, self.project_id, **kwargs)["secret"]

    # -- signed HTTP ----------------------------------------------------------

    def sender(self):
        """A LinkTransport sender speaking to the in-process app."""
        http = self.http

        def send(method, url, headers, body, timeout):
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            self.requests.append({"method": method, "path": path})
            if method == "GET":
                response = http.get(path, headers=headers)
            else:
                response = http.request(method, path, content=body,
                                        headers=headers)
            return response.status_code, response.content

        return send

    def raw_sender(self):
        """A sender whose GET drops query strings (negative tests)."""
        http = self.http

        def send(method, url, headers, body, timeout):
            from urllib.parse import urlparse
            path = urlparse(url).path
            if method == "GET":
                response = http.get(path, headers=headers)
            else:
                response = http.request(method, path, content=body,
                                        headers=headers)
            return response.status_code, response.content

        return send

    def close(self) -> None:
        if self.plane.running:
            self.plane.stop()


def make_client_config(tmp: Path, secret: str, *, client_id: str = "g560",
                       project_id: str = "demo", mode: str = "server",
                       **overrides: Any) -> ClientConfig:
    config = ClientConfig(
        server_url=overrides.pop("server_url", "http://forge-server:8000"),
        client_id=client_id,
        project_id=project_id,
        mode=mode,
        config_dir=str(tmp / "client"),
        **overrides)
    config.store_secret(secret)
    return config


def make_client_for(env: LinkEnv, secret: str, **kwargs) -> Any:
    """A full ForgeClient wired to the in-process server."""
    from forge.client.client import ForgeClient

    config = make_client_config(env.tmp, secret, **kwargs)
    transport = LinkTransport(config, sender=env.sender())
    return ForgeClient(config, transport=transport)


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.1):
    import time

    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting: {predicate} (last={last!r})")
