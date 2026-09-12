"""A81 server management CLI tests (``python -m forge.server``).

Runs the CLI in-process against a temporary control-plane database.
The one-time secret is captured from stdout; nothing secret may persist.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.server import manage
from helpers_link import LinkEnv


@pytest.fixture()
def server(tmp_path, capsys):
    env = LinkEnv(tmp_path / "env")
    env.make_repo()
    manage_args = {"db": env.plane.config.db_path}
    yield env, manage_args
    env.close()


def _run(argv, server):
    env, base = server
    repo = Path(env.plane.projects["demo"].root)
    argv = [a.replace("DEMO_ROOT", str(repo)) for a in argv]
    return manage.main(["--db", base["db"]] + argv)


def test_add_client_prints_secret_once(server, capsys):
    env, base = server
    code = _run(["add-client", "g560", "--project", "demo=DEMO_ROOT",
                 "--max-mode", "assisted"], server)
    assert code == 0
    out = capsys.readouterr().out
    assert "secret" in out and "ONCE" in out
    # The printed secret verifies against the stored verifier.
    secret_line = [line for line in out.splitlines()
                   if line.startswith("secret")][0]
    secret = secret_line.split(":", 1)[1].strip()
    stored = env.service.store.get_client("g560")
    from forge.link import protocol
    assert stored["verifier"] == protocol.derive_verifier(
        secret, stored["salt"])
    # The DB dump contains no plaintext secret.
    assert secret not in json.dumps(env.service.list_clients())


def test_add_client_requires_known_project(server, capsys):
    with pytest.raises(SystemExit):
        _run(["add-client", "g560", "--project", "ghost=x/y"], server)


def test_list_rotate_revoke_lifecycle(server, capsys):
    env, base = server
    assert _run(["add-client", "g560", "--project", "demo=DEMO_ROOT"], server) == 0
    capsys.readouterr()

    assert _run(["list-clients"], server) == 0
    out = capsys.readouterr().out
    assert "g560" in out and "active" in out
    assert "secret" not in out

    assert _run(["rotate-secret", "g560"], server) == 0
    rotated = capsys.readouterr().out
    new_secret = [line for line in rotated.splitlines()
                  if line.startswith("secret")][0].split(":", 1)[1].strip()
    from forge.link import protocol
    stored = env.service.store.get_client("g560")
    assert stored["verifier"] == protocol.derive_verifier(
        new_secret, stored["salt"])

    assert _run(["revoke-client", "g560"], server) == 0
    capsys.readouterr()
    assert _run(["list-clients"], server) == 0
    assert "revoked" in capsys.readouterr().out

    with pytest.raises(Exception):
        _run(["revoke-client", "unknown"], server)


def test_add_client_rejects_bad_ids(server):
    with pytest.raises(Exception):
        _run(["add-client", "Bad Id!", "--project", "demo=DEMO_ROOT"], server)
