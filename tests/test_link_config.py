"""A81 client configuration tests: settings validation, secret storage.

Key guarantees under test:

- the settings JSON never contains the secret;
- the secret file is written with 0600 permissions;
- server URLs with credentials / bad schemes are rejected;
- the reconnect policy ladder is deterministic;
- save/load round-trips.
"""
from __future__ import annotations

import json
import stat

import pytest

from forge.client.config import (
    ClientConfig,
    ConfigError,
    LocalPolicy,
    ReconnectPolicy,
    normalize_server_url,
    validate_client_id,
)


# -- server URL -----------------------------------------------------------------

def test_server_url_normalization():
    assert normalize_server_url("http://Server:8000/") == \
        "http://Server:8000"
    assert normalize_server_url("  https://forge.example.com  ") == \
        "https://forge.example.com"


@pytest.mark.parametrize("bad", [
    "",
    "ftp://server",
    "server:8000",
    "http://user:pass@server:8000",
    "https://token@host/path",
    "http://server/api?x=1",
    "http://server/page#frag",
])
def test_server_url_rejects_bad_input(bad):
    with pytest.raises(ConfigError):
        normalize_server_url(bad)


# -- client id ---------------------------------------------------------------------

def test_client_id_validation():
    assert validate_client_id("G560") == "g560"  # lowercased, accepted
    assert validate_client_id(" g560 ") == "g560"
    for bad in ("", "a b", "a/b", "x" * 33, "a_b"):
        with pytest.raises(ConfigError):
            validate_client_id(bad)


# -- construction validation ------------------------------------------------------------

def test_config_defaults_and_mode_validation():
    config = ClientConfig(server_url="http://s:8000", client_id="g560")
    assert config.mode == "hybrid"
    assert config.reconnect.initial_delay == 1.0
    with pytest.raises(ConfigError):
        ClientConfig(server_url="http://s:8000", client_id="g560",
                     mode="teleport")
    with pytest.raises(ConfigError):
        ClientConfig(server_url="http://s:8000", client_id="g560",
                     request_timeout=0.1)
    with pytest.raises(ConfigError):
        ClientConfig(server_url="http://s:8000", client_id="g560",
                     heartbeat_seconds=0.5)


def test_policy_validation():
    with pytest.raises(ConfigError):
        ReconnectPolicy(initial_delay=0)
    with pytest.raises(ConfigError):
        ReconnectPolicy(multiplier=0.5)
    with pytest.raises(ConfigError):
        ReconnectPolicy(max_attempts=-1)
    with pytest.raises(ConfigError):
        LocalPolicy(max_task_chars=0)


# -- secret storage -----------------------------------------------------------------------

def test_secret_round_trip_and_permissions(tmp_path):
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    path = config.store_secret("A" * 43 + "b")
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, "secret file must be user-only"
    assert config.load_secret() == "A" * 43 + "b"


def test_secret_never_in_settings_file(tmp_path):
    secret = "A" * 43 + "b"
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    config.store_secret(secret)
    saved = config.save()
    raw = saved.read_text(encoding="utf-8")
    assert secret not in raw
    payload = json.loads(raw)
    assert "secret" not in json.dumps(payload)
    # and the save() internal assertion holds
    config2 = ClientConfig.from_dict(payload, config_dir=str(tmp_path))
    assert config2.client_id == "g560"
    assert config2.load_secret() == secret


def test_load_missing_config_is_actionable(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        ClientConfig.load(str(tmp_path))
    assert "no client configuration" in str(excinfo.value)


def test_load_rejects_corrupt_config(tmp_path):
    (tmp_path / "desktop_client.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        ClientConfig.load(str(tmp_path))


def test_round_trip_all_fields(tmp_path):
    config = ClientConfig(
        server_url="https://server:8443", client_id="g560",
        project_id="demo", mode="local", request_timeout=42.0,
        heartbeat_seconds=7.0,
        reconnect=ReconnectPolicy(initial_delay=2.0, max_delay=60.0,
                                  multiplier=3.0, max_attempts=5),
        local=LocalPolicy(allow_local=True, allow_server=False,
                          max_task_chars=100, min_free_ram_mb=256,
                          max_files_walked=10),
        config_dir=str(tmp_path))
    payload = config.to_dict()
    restored = ClientConfig.from_dict(payload, config_dir=str(tmp_path))
    assert restored.server_url == "https://server:8443"
    assert restored.mode == "local"
    assert restored.request_timeout == 42.0
    assert restored.reconnect == config.reconnect
    assert restored.local == config.local


def test_from_dict_reports_bad_values(tmp_path):
    with pytest.raises(ConfigError):
        ClientConfig.from_dict({"server_url": "http://s", "client_id": "g560",
                                "mode": 12}, config_dir=str(tmp_path))
    with pytest.raises(ConfigError):
        ClientConfig.from_dict({"server_url": "http://s",
                                "client_id": "g560",
                                "request_timeout": "fast"},
                               config_dir=str(tmp_path))


# -- reconnect policy ladder -----------------------------------------------------------------

def test_reconnect_ladder_is_deterministic_and_capped():
    policy = ReconnectPolicy(initial_delay=1.0, max_delay=8.0,
                             multiplier=2.0, max_attempts=0)
    assert [policy.delay_for(i) for i in range(6)] == \
        [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert not policy.exhausted(100)          # 0 = retry forever
    bounded = ReconnectPolicy(initial_delay=1.0, max_delay=8.0,
                              multiplier=2.0, max_attempts=3)
    assert not bounded.exhausted(2)
    assert bounded.exhausted(3)


def test_secret_file_missing_message_mentions_setup(tmp_path):
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    with pytest.raises(ConfigError) as excinfo:
        config.load_secret()
    assert "add-client" in str(excinfo.value)
