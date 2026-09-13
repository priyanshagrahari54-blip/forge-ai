"""CLI surface tests for `forge server` (A81).

Covers the pure helpers (project specs, db/url/token resolution), the
argparse wiring (defaults, flags before subcommands), `forge server
start` configuration building + bootstrap-token generation, the
status/health output matrix (including unreachable and 401 paths), and
one real HTTP roundtrip against a live uvicorn-hosted ForgeServer.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from forge import cli  # noqa: E402


def _namespace(**kwargs):
    base = dict(host="", port=0, db="", url="", token="", json=False,
                projects=[], workers=0, profile="",
                server_subcommand=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


# -- pure helpers -----------------------------------------------------------------


def test_parse_projects_valid_and_invalid():
    parser = argparse.ArgumentParser(prog="forge")
    assert cli._server_parse_projects([], parser) == {}
    assert cli._server_parse_projects(
        ["demo=/tmp/demo", "other=/tmp/other"], parser) == {
        "demo": "/tmp/demo", "other": "/tmp/other"}
    with pytest.raises(SystemExit):
        cli._server_parse_projects(["bogus-spec"], parser)
    with pytest.raises(SystemExit):
        cli._server_parse_projects(["=root-only"], parser)


def test_db_path_and_base_url_defaults():
    args = _namespace()
    assert cli._server_db_path(args) == cli.SERVER_DEFAULT_DB
    assert cli._server_base_url(args) == (
        "http://%s:%d/api/v1"
        % (cli.SERVER_DEFAULT_HOST, cli.SERVER_DEFAULT_PORT))
    args = _namespace(host="0.0.0.0", port=9001)
    assert cli._server_base_url(args) == "http://0.0.0.0:9001/api/v1"
    args = _namespace(url="http://example.com/api/v1/")
    assert cli._server_base_url(args) == "http://example.com/api/v1"


def test_token_file_sits_next_to_the_database():
    args = _namespace(db=str(Path("some") / "dir" / "server.db"))
    token_file = Path(cli._server_token_file(args))
    assert token_file.name == "token"
    assert str(token_file.parent) == str(Path("some") / "dir")


def test_resolve_token_precedence(monkeypatch, tmp_path):
    db = tmp_path / "server.db"
    (tmp_path / "token").write_text("file-token\n", encoding="utf-8")
    args = _namespace(db=str(db))
    # 1. explicit --token wins,
    assert cli._server_resolve_token(_namespace(token="explicit",
                                                db=str(db))) == "explicit"
    # 2. then FORGE_SERVER_TOKEN,
    monkeypatch.setenv("FORGE_SERVER_TOKEN", "env-token")
    assert cli._server_resolve_token(args) == "env-token"
    # 3. then the token file next to the db,
    monkeypatch.delenv("FORGE_SERVER_TOKEN")
    assert cli._server_resolve_token(args) == "file-token"
    # 4. empty when nothing is available.
    assert cli._server_resolve_token(_namespace(db=str(tmp_path / "x"
                                                       / "s.db"))) == ""


# -- argparse wiring ----------------------------------------------------------------


def test_main_parses_server_defaults(monkeypatch):
    captured = {}

    def spy(args, parser):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "_run_server", spy)
    with patch.object(sys, "argv", ["forge", "server"]):
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    assert exit_info.value.code == 0
    args = captured["args"]
    assert args.command == "server"
    assert args.server_subcommand is None
    assert args.port == 0 and args.host == ""
    assert args.projects == []


def test_flags_before_subcommand_are_preserved(monkeypatch):
    captured = {}

    def spy(args, parser):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "_run_server", spy)
    with patch.object(sys, "argv",
                      ["forge", "server", "--port", "9123", "--db",
                       "/tmp/x/server.db", "status", "--json"]):
        with pytest.raises(SystemExit):
            cli.main()
    args = captured["args"]
    assert args.server_subcommand == "status"
    # SUPPRESS defaults keep the parent-level values intact.
    assert args.port == 9123
    assert args.db == "/tmp/x/server.db"
    assert args.json is True


def test_run_server_dispatch_table(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_run_server_start",
                        lambda args, parser: calls.append("start") or 0)
    monkeypatch.setattr(cli, "_run_server_status",
                        lambda args: calls.append("status") or 0)
    monkeypatch.setattr(cli, "_run_server_health",
                        lambda args: calls.append("health") or 0)
    parser = argparse.ArgumentParser(prog="forge")
    assert cli._run_server(_namespace(server_subcommand=None), parser) == 0
    assert cli._run_server(_namespace(server_subcommand="start"), parser) == 0
    assert cli._run_server(_namespace(server_subcommand="status"), parser) == 0
    assert cli._run_server(_namespace(server_subcommand="health"), parser) == 0
    assert calls == ["start", "start", "status", "health"]


# -- forge server start ----------------------------------------------------------------


class _FakeServer:
    instances = []

    def __init__(self, config):
        self.config = config
        self.uvicorn_called = False
        _FakeServer.instances.append(self)

    def run_uvicorn(self):
        self.uvicorn_called = True


@pytest.fixture()
def fake_server(monkeypatch):
    _FakeServer.instances = []
    import forge.server as server_module
    monkeypatch.setattr(server_module, "ForgeServer", _FakeServer)
    return _FakeServer


def test_start_generates_token_file_and_defaults(fake_server, tmp_path,
                                                 monkeypatch, capsys):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    db = tmp_path / "state" / "server.db"
    parser = argparse.ArgumentParser(prog="forge")
    args = _namespace(db=str(db))
    assert cli._run_server_start(args, parser) == 0

    created = fake_server.instances[0]
    config = created.config
    assert created.uvicorn_called
    assert config.host == cli.SERVER_DEFAULT_HOST
    assert config.port == cli.SERVER_DEFAULT_PORT
    assert config.profile == "assisted"
    assert config.max_workers == 4
    # Default project = current directory.
    assert config.projects == {"my-repo": str(repo)}

    token_file = Path(cli._server_token_file(args))
    assert token_file.exists()
    token = token_file.read_text(encoding="utf-8")
    assert token and token == config.bootstrap_token
    if os.name == "posix":
        assert (token_file.stat().st_mode & 0o777) == 0o600
    out = capsys.readouterr().out
    assert "bootstrap token" in out
    assert token in out


def test_start_honours_explicit_flags(fake_server, tmp_path, monkeypatch,
                                      capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    parser = argparse.ArgumentParser(prog="forge")
    args = _namespace(host="0.0.0.0", port=9500,
                      db=str(tmp_path / "s.db"),
                      projects=["demo=%s" % (tmp_path / "proj")],
                      workers=2, profile="autonomous", token="fsx_fixed")
    assert cli._run_server_start(args, parser) == 0
    config = fake_server.instances[0].config
    assert config.host == "0.0.0.0" and config.port == 9500
    assert config.projects == {"demo": str(tmp_path / "proj")}
    assert config.max_workers == 2
    assert config.profile == "autonomous"
    assert config.bootstrap_token == "fsx_fixed"
    # An explicit token is never written to disk.
    assert not Path(cli._server_token_file(args)).exists()


def test_start_workers_floor_is_one(fake_server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser(prog="forge")
    cli._run_server_start(_namespace(workers=0,
                                     db=str(tmp_path / "s.db")), parser)
    assert fake_server.instances[0].config.max_workers == 4
    cli._run_server_start(_namespace(workers=-3,
                                     db=str(tmp_path / "s2.db")), parser)
    assert fake_server.instances[1].config.max_workers == 1


def test_start_rejects_bad_profile_and_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser(prog="forge")
    with pytest.raises(SystemExit):
        cli._run_server_start(_namespace(profile="yolo"), parser)
    with pytest.raises(SystemExit):
        cli._run_server_start(_namespace(projects=["nope"]), parser)


# -- status / health output matrix ------------------------------------------------------


_STATUS_PAYLOAD = {
    "server": "forge-server", "version": "1.2.3", "boot_id": "boot-9",
    "uptime_seconds": 12.5, "profile": "assisted",
    "workers": {"busy": 1, "max": 4, "alive": True},
    "queue": {"depth": 2, "leased": 1},
    "tasks": {"running": 1, "queued": 2, "completed": 0},
    "projects": 3, "active_sessions": 1, "pending_approvals": 1,
}

_HEALTH_PAYLOAD = {
    "status": "ok",
    "server": {"version": "1.2.3", "boot_id": "boot-9",
               "uptime_seconds": 3.0},
    "auth_mode": "local-dev", "profile": "assisted",
    "components": {
        "database": {"ok": True},
        "workers": {"ok": True, "busy": 0, "max": 4},
        "disk": {"ok": True, "free_mb": 1234},
    },
    "warnings": [],
}


def test_status_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (200, dict(_STATUS_PAYLOAD)))
    assert cli._run_server_status(_namespace()) == 0
    out = capsys.readouterr().out
    assert "Forge Server: running" in out
    assert "1.2.3" in out and "boot-9" in out
    assert "1/4 busy" in out
    assert "2 queued" in out
    assert "'running': 1" in out
    assert "3 registered" in out


def test_status_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (0, {"error": "connection refused"}))
    assert cli._run_server_status(_namespace()) == 1
    out = capsys.readouterr().out
    assert "not running" in out
    assert "connection refused" in out


def test_status_bad_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (401, {"error": {"code": "UNAUTHENTICATED"}}))
    assert cli._run_server_status(_namespace()) == 1
    out = capsys.readouterr().out
    assert "refused the credentials" in out
    assert "FORGE_SERVER_TOKEN" in out


def test_status_http_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0: (503, {}))
    assert cli._run_server_status(_namespace()) == 1
    assert "HTTP 503" in capsys.readouterr().out


def test_status_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (200, dict(_STATUS_PAYLOAD)))
    assert cli._run_server_status(_namespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reachable"] is True
    assert payload["http_status"] == 200
    assert payload["payload"]["boot_id"] == "boot-9"


def test_health_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (200, dict(_HEALTH_PAYLOAD)))
    assert cli._run_server_health(_namespace()) == 0
    out = capsys.readouterr().out
    assert "Forge Server health: OK" in out
    assert "database" in out and "workers" in out
    assert "auth mode: local-dev" in out


def test_health_degraded_is_success_with_warning(monkeypatch, capsys):
    payload = json.loads(json.dumps(_HEALTH_PAYLOAD))
    payload["status"] = "degraded"
    payload["components"]["disk"] = {"ok": False, "free_mb": 10}
    payload["warnings"] = ["disk space low"]
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0: (200, payload))
    assert cli._run_server_health(_namespace()) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out
    assert "FAIL" in out
    assert "warning: disk space low" in out


def test_health_down_exits_nonzero(monkeypatch, capsys):
    payload = json.loads(json.dumps(_HEALTH_PAYLOAD))
    payload["status"] = "down"
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0: (200, payload))
    assert cli._run_server_health(_namespace()) == 1
    assert "DOWN" in capsys.readouterr().out


def test_health_bad_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0: (401, {}))
    assert cli._run_server_health(_namespace()) == 1
    out = capsys.readouterr().out
    assert "HTTP 401" in out
    assert "FORGE_SERVER_TOKEN" in out


def test_health_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_http_get",
                        lambda url, token, timeout=5.0:
                        (200, dict(_HEALTH_PAYLOAD)))
    assert cli._run_server_health(_namespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["payload"]["status"] == "ok"


# -- live roundtrip ---------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_status_and_health_against_live_server(tmp_path, monkeypatch,
                                               capsys):
    import uvicorn

    from forge.server import ForgeServer, ServerConfig

    repo = tmp_path / "demo"
    repo.mkdir()
    port = _free_port()
    config = ServerConfig(
        db_path=str(tmp_path / "server.db"),
        host="127.0.0.1", port=port,
        projects={"demo": str(repo)},
        bootstrap_token="cli_bootstrap_token_123",
        max_workers=1)
    server = ForgeServer(config)
    server.start()
    uvicorn_config = uvicorn.Config(server.create_app(),
                                    host="127.0.0.1", port=port,
                                    log_level="warning")
    httpd = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=httpd.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not httpd.started:
            time.sleep(0.05)
        assert httpd.started, "uvicorn did not start"

        args = _namespace(port=port, token="cli_bootstrap_token_123")
        assert cli._run_server_status(args) == 0
        out = capsys.readouterr().out
        assert "Forge Server: running" in out
        assert "127.0.0.1:%d" % port in out

        assert cli._run_server_health(args) == 0
        out = capsys.readouterr().out
        assert "Forge Server health: OK" in out

        # FORGE_SERVER_TOKEN works for clients (no --token flag).
        monkeypatch.setenv("FORGE_SERVER_TOKEN", "cli_bootstrap_token_123")
        assert cli._run_server_status(_namespace(port=port)) == 0
        capsys.readouterr()

        # Wrong credentials are refused end to end.
        monkeypatch.setenv("FORGE_SERVER_TOKEN", "fsk_wrong")
        assert cli._run_server_status(_namespace(port=port)) == 1
        assert "refused the credentials" in capsys.readouterr().out
    finally:
        httpd.should_exit = True
        thread.join(timeout=15)
        server.close()


def test_status_unreachable_live(tmp_path, capsys):
    port = _free_port()
    assert cli._run_server_status(_namespace(port=port)) == 1
    assert "not running" in capsys.readouterr().out
