import json

import pytest

from forge.models.credentials import CredentialError, CredentialStore


def test_resolves_from_environment():
    store = CredentialStore(env={"OPENAI_API_KEY": "sk-test-123"})
    assert store.configured("openai") is True
    assert store.get("openai") == "sk-test-123"
    assert store.require("openai") == "sk-test-123"
    assert store.configured("ollama") is False


def test_require_raises_when_missing():
    store = CredentialStore(env={})
    with pytest.raises(CredentialError):
        store.require("openai")


def test_repr_never_leaks_secrets():
    store = CredentialStore(env={"OPENAI_API_KEY": "sk-super-secret-value"})
    assert "sk-super-secret-value" not in repr(store)
    assert "sk-super-secret-value" not in json.dumps(store.providers())


def test_file_with_loose_permissions_rejected(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"openai": "sk-file"}))
    path.chmod(0o644)
    with pytest.raises(CredentialError):
        CredentialStore(env={}, files=(path,))


def test_file_with_safe_permissions_read(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"openai": "sk-file"}))
    path.chmod(0o600)
    store = CredentialStore(env={}, files=(path,))
    assert store.get("openai") == "sk-file"


def test_non_json_file_rejected(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("not json")
    path.chmod(0o600)
    with pytest.raises(CredentialError):
        CredentialStore(env={}, files=(path,))
