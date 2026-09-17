"""HTTP integration tests for the evidence-backed capability registry."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import TEST_TOKEN, auth_headers, make_client, make_server, success_executor


ADMIN = auth_headers(TEST_TOKEN)


def test_capabilities_requires_authentication(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    finally:
        server.close()


def test_capabilities_returns_truth_snapshot(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/capabilities", headers=ADMIN)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["schema_version"] == "1"
            assert payload["honesty_contract"]
            capabilities = {item["id"]: item for item in payload["capabilities"]}
            assert "model-fabric" in capabilities
            assert "persistent-server" in capabilities
            assert capabilities["persistent-server"]["state"] == "LIVE"
            assert capabilities["model-fabric"]["state"] == "LIVE"
            # Architecture/simulation must never be promoted to live merely
            # because the endpoint itself is reachable.
            assert capabilities["remote-compute"]["state"] == "ARCHITECTURE"
            assert capabilities["voice"]["state"] == "SIMULATED"
    finally:
        server.close()


def test_capabilities_uses_existing_models_status_scope(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            key_response = client.post(
                "/api/v1/auth/keys", headers=ADMIN,
                json={"name": "viewer-capabilities", "role": "viewer"})
            assert key_response.status_code == 200, key_response.text
            viewer = auth_headers(key_response.json()["key"]["key"])
            response = client.get("/api/v1/capabilities", headers=viewer)
            assert response.status_code == 200, response.text
    finally:
        server.close()
