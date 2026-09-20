from forge.api.app import create_app
from forge.control import ControlPlane, ControlConfig
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from fastapi.testclient import TestClient


class RuntimeProvider:
    name = "runtime"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("OK", "runtime/model-a")


def make_plane(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(
                name="runtime/model-a",
                provider="runtime",
                capabilities=("coding",),
            ),
        ]),
        providers=ProviderRegistry({"runtime": RuntimeProvider()}),
    )
    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        fabric=fabric,
    )
    return ControlPlane(config)


def login(client):
    response = client.post(
        "/api/v1/sessions",
        json={"actor": "alice", "project_id": "demo", "profile": "assisted"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_runtime_inference_check_promotes_exact_configured_runtime(tmp_path):
    plane = make_plane(tmp_path)
    with TestClient(create_app(plane)) as client:
        headers = login(client)
        response = client.post(
            "/api/v1/runtimes/inference-check",
            json={"provider": "runtime", "model_id": "runtime/model-a"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["state"] == "LIVE"
        assert payload["probe"]["reason"] == "real inference probe succeeded"


def test_runtime_inference_check_requires_exact_runtime_identity(tmp_path):
    plane = make_plane(tmp_path)
    with TestClient(create_app(plane)) as client:
        headers = login(client)
        response = client.post(
            "/api/v1/runtimes/inference-check",
            json={"provider": "runtime", "model_id": "runtime/unknown"},
            headers=headers,
        )

        assert response.status_code == 404
        payload = response.json()
        assert payload["error"]["code"] == "NOT_FOUND"
        assert "configured runtime not found" in payload["error"]["message"]
