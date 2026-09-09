"""Model Fabric bridge (A46): the plane's gated path from agent to
model with honest routing metadata."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.models.bridge import FabricBridge  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.models.provider import (  # noqa: E402
    LocalModelProvider,
    ModelResult,
    ProviderInfo,
    ProviderRegistry,
)
from forge.models.registry import Model, ModelRegistry  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def model_policy(effect: str = "ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-call", resource=Resource.MODEL,
                       operation="call", scope="", effect=effect,
                       capability="coding"),
    ])


def noop_fabric() -> ModelFabric:
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="local-default", provider="local",
                  capabilities=("coding",), free=True, local=True),
        ]),
        providers=ProviderRegistry({"local": LocalModelProvider()}),
    )


class FakeRemote:
    name = "fake-remote"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("remote output", self.name)


class FakeMock:
    name = "fake-mock"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("mock output", self.name)


def test_bridge_marks_noop_local_as_simulated():
    bridge = FabricBridge(noop_fabric())
    response = bridge.generate("write a loop", capability="coding")
    assert response["success"] is True
    assert response["provider"] == "local"
    assert response["provider_kind"] == "local"
    assert response["simulated"] is True
    # The local provider refuses to fabricate and says so.
    assert "configure" in response["text"].lower()
    with pytest.raises(ValueError):
        bridge.generate("")


def _fabric_with(models, providers_with_info) -> ModelFabric:
    provider_registry = ProviderRegistry()
    for name, (provider, info) in providers_with_info.items():
        provider_registry.register(name, provider, info=info)
    return ModelFabric(registry=ModelRegistry(models),
                       providers=provider_registry)


def test_bridge_marks_mock_and_remote_honestly():
    mock_fabric = _fabric_with(
        [Model(name="m/mock", provider="fake-mock",
               capabilities=("coding",), free=True, local=True)],
        {"fake-mock": (FakeMock(),
                       ProviderInfo(name="fake-mock", kind="mock")),
         "fake-remote": (FakeRemote(),
                         ProviderInfo(name="fake-remote", kind="remote"))})
    bridge = FabricBridge(mock_fabric)
    mock_response = bridge.generate("anything", capability="coding")
    assert mock_response["provider"] == "fake-mock"
    assert mock_response["provider_kind"] == "mock"
    assert mock_response["simulated"] is True

    remote_fabric = _fabric_with(
        [Model(name="m/remote", provider="fake-remote",
               capabilities=("coding",), free=False, local=False)],
        {"fake-remote": (FakeRemote(),
                         ProviderInfo(name="fake-remote", kind="remote"))})
    remote_response = FabricBridge(remote_fabric).generate(
        "anything", capability="coding")
    assert remote_response["provider"] == "fake-remote"
    assert remote_response["provider_kind"] == "remote"
    assert remote_response["simulated"] is False


def test_plane_generate_is_policy_gated_and_audited(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.model_generate(session, "summarize the plan",
                                      capability="coding")
        assert result["allowed"] is True
        assert result["response"]["provider"] == "p"
        assert result["response"]["capability"] == "coding"
        assert result["response"]["success"] is True
        audited = plane.audit.query(resource="model")
        assert audited and audited[-1].decision.value == "ALLOW"
        assert "provider=p" in audited[-1].reason


def test_plane_generate_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy("DENY"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.model_generate(session, "anything")
        assert result["allowed"] is False
        assert result["response"] is None
        assert "deny" in result["reason"].lower()


def test_plane_generate_approval_round_trip(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=model_policy("REQUIRE_APPROVAL"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        first = plane.model_generate(session, "refactor the queue")
        assert first["allowed"] is False
        approval_id = first["approval_request_id"]
        assert any(item["id"] == approval_id
                   for item in plane.list_model_approvals(session))
        decided = plane.decide_model_approval(session, approval_id, True)
        second = plane.model_generate(
            session, "refactor the queue",
            approval_id=decided["token_id"])
        assert second["allowed"] is True
        assert second["response"]["success"] is True
        # Spent token refiles a fresh approval, never silently allows.
        replay = plane.model_generate(
            session, "refactor the queue",
            approval_id=decided["token_id"])
        assert replay["allowed"] is False
        assert replay["approval_request_id"] != approval_id


def test_conversation_answers_model_question_from_registry(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.converse(session, "What models do you have?")
        assert result["kind"] == "question"
        assert "model(s)" in result["reply"]
        assert "no-op" in result["reply"]


def test_model_generate_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.post("/api/v1/models/generate",
                           json={"prompt": "x"}).status_code == 401
        _session, _token, headers = login(client)
        generate = client.post("/api/v1/models/generate", headers=headers,
                               json={"prompt": "outline a module"})
        assert generate.status_code == 200
        body = generate.json()
        assert body["allowed"] is True
        assert body["response"]["provider"] == "p"
        assert client.post("/api/v1/models/generate", headers=headers,
                           json={"prompt": ""}).status_code == 400
        approvals = client.get("/api/v1/models/approvals",
                               headers=headers)
        assert approvals.status_code == 200
        assert approvals.json()["approvals"] == []
