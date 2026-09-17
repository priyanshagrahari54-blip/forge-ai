"""HTTP and registry tests for official AI-provider resource links."""
from __future__ import annotations

from forge.models.provider_links import enrich_provider_info, provider_link_catalog, provider_links

from helpers_server import TEST_TOKEN, auth_headers, make_client, make_server, success_executor


ADMIN = auth_headers(TEST_TOKEN)


def test_known_provider_links_are_official_and_public():
    links = provider_links("openai")
    assert links["website"] == "https://openai.com/"
    assert links["api"] == "https://platform.openai.com/"
    assert links["docs"].startswith("https://platform.openai.com/")


def test_catalog_contains_multiple_provider_families():
    names = {item["name"] for item in provider_link_catalog()}
    assert {"openai", "anthropic", "google", "mistral", "ollama", "huggingface", "openrouter"} <= names


def test_unknown_provider_does_not_get_an_invented_link():
    assert provider_links("unknown-provider") == {}
    assert "links" not in enrich_provider_info({"name": "unknown-provider"})


def test_provider_links_requires_authentication(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/provider-links")
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    finally:
        server.close()


def test_provider_links_endpoint_returns_catalog(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/provider-links", headers=ADMIN)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["schema_version"] == 1
            providers = {item["name"]: item for item in payload["providers"]}
            assert "openai" in providers
            assert providers["openai"]["links"]["website"] == "https://openai.com/"
            assert providers["openai"]["links"]["api"] == "https://platform.openai.com/"
    finally:
        server.close()


def test_provider_links_uses_existing_models_status_scope(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            key_response = client.post(
                "/api/v1/auth/keys",
                headers=ADMIN,
                json={"name": "viewer-provider-links", "role": "viewer"},
            )
            assert key_response.status_code == 200, key_response.text
            viewer = auth_headers(key_response.json()["key"]["key"])
            response = client.get("/api/v1/provider-links", headers=viewer)
            assert response.status_code == 200, response.text
    finally:
        server.close()
