"""Cockpit provider view includes only known official provider links."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane


def test_cockpit_providers_expose_official_links(tmp_path):
    plane = make_plane(tmp_path)
    client = make_client(plane)
    try:
        with client:
            _, _, headers = login(client)
            response = client.get("/api/v1/providers", headers=headers)
            assert response.status_code == 200, response.text
            providers = {item["name"]: item for item in response.json()["providers"]}
            assert providers
            if "openai" in providers:
                links = providers["openai"].get("links", {})
                assert links["website"] == "https://openai.com/"
                assert links["api"] == "https://platform.openai.com/"
                assert links["docs"].startswith("https://platform.openai.com/")
    finally:
        plane.close()
