"""A81 hardening tests: abuse control, races, redirects, contracts.

Every test here pins a hardening fix:

- per-client handshake lockout after repeated failures;
- race-safe nonce replay cache;
- rate-limited unauthenticated handshake routes;
- redirect refusal in the transport (signature-header leak);
- typed 400s for garbage query params (never a 500);
- snapshot()/poll contract: UI refresh never raises;
- event-cursor paging: no event is ever skipped;
- fail-closed resource probing;
- secret-file permission enforcement;
- runtime (not assert) guard against secrets in the settings file;
- bounded, loop-safe local repository walk.
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from forge.client.config import ClientConfig, ConfigError, LocalPolicy
from forge.client.local_exec import LocalExecutor
from forge.client.resources import probe
from forge.link import protocol
from forge.link.errors import AuthError, ServerError
from helpers_link import LinkEnv, make_client_config


@pytest.fixture()
def env(tmp_path):
    environment = LinkEnv(tmp_path)
    environment.make_repo()
    yield environment
    environment.close()


def _handshake(env, secret, client_id="g560", *, now=None):
    nonce = protocol.new_nonce()
    challenge = env.http.post("/api/v1/link/challenge",
                              json={"client_id": client_id,
                                    "nonce": nonce}).json()
    verifier = protocol.derive_verifier(secret, challenge["salt"])
    proof = protocol.handshake_proof(verifier, client_id, nonce,
                                     challenge["server_nonce"])
    return env.http.post("/api/v1/link/handshake",
                         json={"client_id": client_id, "nonce": nonce,
                               "proof": proof})


# -- handshake lockout ----------------------------------------------------------

def test_repeated_failures_lock_the_client_out(env):
    secret = env.register()
    for _ in range(env.service.store.MAX_HANDSHAKE_FAILURES):
        response = env.http.post("/api/v1/link/handshake",
                                 json={"client_id": "g560",
                                       "nonce": protocol.new_nonce(),
                                       "proof": "0" * 64})
        assert response.status_code == 401
    # Locked out: even a *correct* handshake is refused while the
    # lockout window is active.
    assert _handshake(env, secret).status_code == 401
    assert env.service.store.handshake_locked("g560", now=time.time())


def test_lockout_expires_and_success_clears_the_counter(env):
    secret = env.register()
    store = env.service.store
    now = time.time()
    # Failures inside the window -> locked; a correct handshake at the
    # wall clock is refused too.
    for i in range(store.MAX_HANDSHAKE_FAILURES):
        store.record_handshake_failure("g560", now=now - 10 + i)
    assert store.handshake_locked("g560", now=now)
    nonce = protocol.new_nonce()
    challenge = env.service.challenge("g560", nonce, now=now + 1)
    verifier = protocol.derive_verifier(secret, challenge["salt"])
    proof = protocol.handshake_proof(verifier, "g560", nonce,
                                     challenge["server_nonce"])
    with pytest.raises(AuthError):
        env.service.handshake("g560", nonce, proof, now=now + 1)

    # After the lockout window passes, the same credentials work and
    # the success clears the failure history.
    later = now + store.HANDSHAKE_LOCKOUT_SECONDS + 1
    assert not store.handshake_locked("g560", now=later)
    nonce2 = protocol.new_nonce()
    challenge2 = env.service.challenge("g560", nonce2, now=later)
    verifier2 = protocol.derive_verifier(secret, challenge2["salt"])
    proof2 = protocol.handshake_proof(verifier2, "g560", nonce2,
                                      challenge2["server_nonce"])
    result = env.service.handshake("g560", nonce2, proof2, now=later)
    assert result["client_id"] == "g560"
    assert not store.handshake_locked("g560", now=later + 1)


def test_lockout_never_applies_to_unknown_clients(env):
    # Unknown ids fail auth without touching the failure table.
    response = env.http.post("/api/v1/link/handshake",
                             json={"client_id": "ghost",
                                   "nonce": protocol.new_nonce(),
                                   "proof": "0" * 64})
    assert response.status_code == 401
    assert env.service.store.get_client("ghost") is None


def test_failed_handshakes_are_audited(env):
    env.register()
    env.http.post("/api/v1/link/handshake",
                  json={"client_id": "g560", "nonce": protocol.new_nonce(),
                        "proof": "0" * 64})
    entries = env.plane.audit.to_dict()
    assert any(e.get("operation") == "handshake"
               and e.get("decision") == "DENY" for e in entries)


# -- nonce replay cache race -------------------------------------------------------

def test_nonce_cache_is_race_safe(env):
    store = env.service.store
    now = time.time()
    assert store.nonce_seen("g560", "a" * 32, now=now, window=120) is False
    # A concurrent duplicate must be reported as seen (never crash with
    # an IntegrityError through to the API).
    assert store.nonce_seen("g560", "a" * 32, now=now, window=120) is True
    assert store.nonce_seen("g560", "a" * 32, now=now, window=120) is True


# -- unauthenticated-route rate limit ----------------------------------------------

def test_challenge_is_rate_limited(env):
    statuses = []
    for _ in range(35):
        response = env.http.post("/api/v1/link/challenge",
                                 json={"client_id": "g560",
                                       "nonce": protocol.new_nonce()})
        statuses.append(response.status_code)
    assert 429 in statuses


# -- redirect refusal (signature-header leak) ----------------------------------------

def test_transport_refuses_redirects():
    from forge.client.transport import _NoRedirect

    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, 
                                    "http://evil.example/") is None
    assert handler.redirect_request(None, None, 307, "Tmp", {},
                                    "http://evil.example/") is None


def test_redirect_status_is_a_transport_error_not_a_follow():
    from forge.client.transport import _parse

    with pytest.raises(ServerError):
        _parse(302, b"{}")


# -- garbage query params are 400s, never 500s --------------------------------------

def _signed(env, key, path):
    ts = str(int(time.time()))
    nonce = protocol.new_nonce()
    sig = protocol.sign_request(key, "GET", path, b"", ts, nonce)
    return env.http.get(path, headers=protocol.signature_headers(
        "g560", ts, nonce, sig))


def _session_key(env, secret=None):
    secret = secret if secret is not None else env.register()
    nonce = protocol.new_nonce()
    challenge = env.http.post("/api/v1/link/challenge",
                              json={"client_id": "g560",
                                    "nonce": nonce}).json()
    verifier = protocol.derive_verifier(secret, challenge["salt"])
    proof = protocol.handshake_proof(verifier, "g560", nonce,
                                     challenge["server_nonce"])
    env.http.post("/api/v1/link/handshake",
                  json={"client_id": "g560", "nonce": nonce,
                        "proof": proof})
    return protocol.session_key(verifier, "g560", nonce,
                                challenge["server_nonce"])


def test_bad_query_params_are_typed_400s(env):
    key = _session_key(env)
    for path in ("/api/v1/link/tasks?limit=abc",
                 "/api/v1/link/tasks?limit=99999",
                 "/api/v1/link/state?since=banana",
                 "/api/v1/link/tasks/t-1/events?after=-5"):
        response = _signed(env, key, path)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "INVALID_REQUEST", path


def test_execution_label_is_validated(env):
    key = _session_key(env)
    body = json.dumps({"requirement": "Add CSV export functionality",
                       "execution": "SERVER<script>"}).encode()
    ts = str(int(time.time()))
    nonce = protocol.new_nonce()
    sig = protocol.sign_request(key, "POST", "/api/v1/link/tasks", body,
                                ts, nonce)
    response = env.http.post("/api/v1/link/tasks", content=body, headers={
        "Content-Type": "application/json",
        **protocol.signature_headers("g560", ts, nonce, sig)})
    assert response.status_code == 200, response.text
    task_id = response.json()["task"]["id"]
    # A hostile label is normalized to the honest default.
    assert env.service.store.get_task_origin(task_id)["execution"] == \
        "SERVER"


# -- snapshot/poll contract ---------------------------------------------------------

def test_snapshot_never_raises_on_auth_failure(tmp_path):
    from forge.client.client import ForgeClient
    from forge.client.transport import LinkTransport
    from forge.link.errors import AuthError

    class ExplodingTransport(LinkTransport):
        def get(self, path, params=None):
            raise AuthError("session expired")

    config = make_client_config(tmp_path, "A" * 43 + "b")
    transport = ExplodingTransport(config, sender=lambda *a: (200, b"{}"))
    client = ForgeClient(config, transport=transport)
    client._server_status = ServerStatusForTest()
    # A poll during an expired session must return a snapshot (with the
    # failure recorded), never raise.
    snapshot = client.snapshot()
    assert snapshot["connection"]["state"] in ("DISCONNECTED",
                                               "RECONNECTING",
                                               "AUTH_FAILED")
    assert "detail" in snapshot["connection"]


def test_snapshot_skips_verification_fetch_while_unreachable(tmp_path):
    from forge.client.client import ForgeClient
    from forge.client.transport import LinkTransport

    calls = {"verification": 0}

    class CountingTransport(LinkTransport):
        def get(self, path, params=None):
            if "/verification" in path:
                calls["verification"] += 1
            raise RuntimeError("dead")

    config = make_client_config(tmp_path, "A" * 43 + "b")
    client = ForgeClient(config,
                         transport=CountingTransport(config,
                                                     sender=lambda *a:
                                                     (200, b"{}")))
    client._server_status = ServerStatusForTest(reachable=False)
    for _ in range(3):
        snapshot = client.snapshot(selected_task="t-1")
        assert "tasks" in snapshot
    assert calls["verification"] == 0  # no doomed requests


class ServerStatusForTest:
    """Stand-in for router.ServerStatus with controllable reachability."""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.model_ready = False
        self.workers = 0


# -- event-cursor paging ------------------------------------------------------------

def test_snapshot_cursor_never_skips_truncated_events(env):
    secret = env.register()
    key = _session_key(env, secret)
    body = json.dumps({"requirement": "Add CSV export functionality"}).encode()
    ts = str(int(time.time()))
    nonce = protocol.new_nonce()
    sig = protocol.sign_request(key, "POST", "/api/v1/link/tasks", body,
                                ts, nonce)
    created = env.http.post("/api/v1/link/tasks", content=body, headers={
        "Content-Type": "application/json",
        **protocol.signature_headers("g560", ts, nonce, sig)})
    task_id = created.json()["task"]["id"]

    # Stuff the task with far more events than one page holds.
    run = env.plane.runs.get(task_id)
    for i in range(230):
        env.plane.events.append(run.id, run.project_id, "note",
                                {"i": str(i)})

    link_session = env.service.store.get_session("g560", now=time.time())
    session = env.plane.sessions.get(link_session["session_id"])
    first = env.service.state_snapshot(session, since_seq=0,
                                       selected_task=task_id)
    assert len(first["events"]) == 200            # one full page
    cursor = first["event_cursor"]
    assert cursor < 230                            # not the store latest
    second = env.service.state_snapshot(session, since_seq=cursor,
                                        selected_task=task_id)
    # The rest: the remaining notes plus any pipeline events emitted
    # after them (both must be present — nothing skipped).
    total = env.plane.events.latest_seq(run.id)
    assert len(first["events"]) + len(second["events"]) == total
    assert total >= 230
    # No gaps, no duplicates across pages: the union is exactly 1..total.
    seqs = [e["seq"] for e in first["events"]] + \
        [e["seq"] for e in second["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == total
    assert seqs == list(range(1, total + 1))


# -- fail-closed resource probing -----------------------------------------------------

def test_probe_failure_fails_closed(tmp_path):
    from forge.client.client import ForgeClient
    from forge.client.transport import LinkTransport

    def broken_probe():
        raise RuntimeError("no /proc")

    config = make_client_config(tmp_path, "A" * 43 + "b", mode="local",
                                project_id="proj")
    client = ForgeClient(config, transport=LinkTransport(
        config, sender=lambda *a: (200, b"{}")), resource_probe=broken_probe)
    decision = client.decide("show repository status")
    assert decision.execution in ("REFUSED", "SERVER")
    assert "resources" in decision.reason or "refused" in decision.reason


def test_probe_reports_unavailable_not_garbage(tmp_path, monkeypatch):
    import forge.client.resources as resources

    # Force every source to fail: no /proc, non-win, non-darwin.
    monkeypatch.setattr(resources.os.path, "exists",
                        lambda p: False, raising=True)
    monkeypatch.setattr(resources.sys, "platform", "sunos")
    snapshot = resources.probe()
    assert snapshot.source == "unavailable"
    assert snapshot.free_ram_mb == 0 and snapshot.total_ram_mb == 0
    assert not snapshot.adequate(min_free_ram_mb=1)


def test_probe_reads_proc_meminfo(tmp_path):
    fake = tmp_path / "meminfo"
    fake.write_text("MemTotal:       8000000 kB\n"
                    "MemAvailable:   2000000 kB\n")
    snapshot = probe(str(fake))
    assert snapshot.source == "proc"
    assert snapshot.total_ram_mb == 7812  # 8_000_000 kB // 1024 // 1024
    assert snapshot.free_ram_mb == 1953


# -- secret-file permission enforcement ------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission model")
def test_world_readable_secret_is_refused(tmp_path):
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    path = config.store_secret("A" * 43 + "b")
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError) as excinfo:
        config.load_secret()
    assert "rotate-secret" in str(excinfo.value)
    # The file was tightened as part of refusing.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission model")
def test_tightened_secret_still_loads(tmp_path):
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    path = config.store_secret("A" * 43 + "b")
    os.chmod(path, 0o600)
    assert config.load_secret() == "A" * 43 + "b"


def test_save_rejects_secret_material_even_without_asserts(tmp_path,
                                                           monkeypatch):
    """The settings guard is runtime, not an assert (survives -O)."""
    config = ClientConfig(server_url="http://s:8000", client_id="g560",
                          config_dir=str(tmp_path))
    monkeypatch.setattr(ClientConfig, "to_dict",
                        lambda self: {"version": 1, "secret": "leaked"})
    with pytest.raises(ConfigError):
        config.save()
    assert not (tmp_path / "desktop_client.json").exists()


# -- bounded, loop-safe local walk -----------------------------------------------------

def test_walk_tolerates_symlink_loops_and_unreadable_dirs(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    loop = root / "loop"
    loop.mkdir()
    (loop / "inside").symlink_to(loop)          # symlink loop
    locked = root / "locked"
    locked.mkdir()
    os.chmod(locked, 0)                          # unreadable (root ignores)
    (root / ".venv").mkdir()
    (root / ".venv" / "v.py").write_text("v = 1\n")
    try:
        report = LocalExecutor(
            LocalPolicy(max_files_walked=500)).run("repo_summary",
                                                   str(root))
        # a.py counted; .venv skipped; the loop did not hang.
        assert report["python_files"] == 1
        assert ".venv" not in str(report["top_extensions"])
    finally:
        os.chmod(locked, 0o755)


def test_walk_is_lazy_not_tree_materializing(tmp_path, monkeypatch):
    """A huge repo must not build a full listing just to bound it."""
    from forge.client.local_exec import LocalExecutor
    import forge.client.local_exec as module

    root = tmp_path / "big"
    root.mkdir()
    for i in range(50):
        (root / f"f{i:03d}.py").write_text("x = 1\n")

    real_scandir = module.os.scandir
    seen = {"scandir": 0}

    def counting_scandir(path):
        seen["scandir"] += 1
        return real_scandir(path)

    monkeypatch.setattr(module.os, "scandir", counting_scandir)
    report = LocalExecutor(
        LocalPolicy(max_files_walked=5)).run("repo_summary", str(root))
    assert report["files_seen"] == 5 and report["truncated"]
    # Bounded: stops after the root plus at most a couple of levels,
    # instead of scanning all directories first.
    assert seen["scandir"] <= 3
