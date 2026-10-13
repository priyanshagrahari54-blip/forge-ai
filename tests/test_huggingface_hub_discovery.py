from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from forge.capabilities.huggingface import HuggingFaceHubDiscovery


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.body[:limit]


class HuggingFaceHubDiscoveryTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_search_returns_metadata_and_marks_unverified(self, open_url):
        open_url.return_value = _Response(json.dumps([{
            "id": "org/model",
            "pipeline_tag": "text-generation",
            "license": "apache-2.0",
            "downloads": 123,
            "likes": 4,
            "lastModified": "2026-01-01",
            "tags": ["text-generation", "apache-2.0"],
        }]).encode())
        models = HuggingFaceHubDiscovery().search_models("reasoning")
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].model_id, "org/model")
        self.assertEqual(models[0].license, "apache-2.0")
        self.assertEqual(models[0].to_dict()["status"], "discovered_unverified")

    def test_empty_query_rejected(self):
        with self.assertRaises(ValueError):
            HuggingFaceHubDiscovery().search_models(" ")

    @patch("urllib.request.urlopen")
    def test_unexpected_response_rejected(self, open_url):
        open_url.return_value = _Response(b'{"unexpected":true}')
        with self.assertRaises(RuntimeError):
            HuggingFaceHubDiscovery().search_models("reasoning")


if __name__ == "__main__":
    unittest.main()
