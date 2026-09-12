"""A81 Forge Server link API tests.

Covers: registration (verifier-only storage), handshake flows, the
signed-request surface, replay/tamper rejection, authoritative
authorization, and the absence of any command-execution surface.
"""
from __future__ import annotations

import json
import time

import pytest

from forge.link import protocol
from forge.link.errors import AuthError, RequestError
from helpers_link import LinkEnv, wait_until  # noqa: F401


@pytest.fixture()
def env(tmp_path):
    environment = LinkEnv(tmp_path)
    environment.make_repo()
    yield environment
    environment.close()


def _handshake(env, secret, client_id="g560"):
    nonce = protocol.new_nonce()
    response = env.http.post("/api/v1/link/challenge",
                             json={"client_id": client_id, "nonce": nonce})
    assert response.status_code == 200, response.text
    payload = response.json()
    verifier = protocol.derive_verifier(secret, payload["salt"])
    proof = protocol.handshake_proof(verifier, client_id, nonce,
                                     payload["server_nonce"])
    response = env.http.post("/api/v1/link/handshake",
                             json={"client_id": client_id, "nonce": nonce,
                                   "proof": proof})
    assert response.status_code == 200, response.text
    key = protocol.session_key(verifier, client_id, nonce,
                               payload["server_nonce"])
    return key, response.json()


def _signed_get(env, key, path, client_id="g560", *, timestamp=None,
                nonce=None, signature=None):
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or protocol.new_nonce()
    signature = signature or protocol.sign_request(key, "GET", path,
                                                   b"", timestamp, nonce)
    return env.http.get(path, headers=protocol.signature_headers(
        client_id, timestamp, nonce, signature))


def _signed_post(env, key, path, payload, client_id="g560", *,
                 timestamp=None, nonce=None, signature=None, body=None):
    body = body if body is not None else json.dumps(payload).encode("utf-8")
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or protocol.new_nonce()
    signature = signature or protocol.sign_request(key, "POST", path, body,
                                                   timestamp, nonce)
    return env.http.post(path, content=body, headers={
        "Content-Type": "application/json",
        **protocol.signature_headers(client_id, timestamp, nonce,
                                     signature)})


# -- registration -----------------------------------------------------------------

def test_registration_returns_secret_once_and_stores_verifier_only(env):
    result = env.service.register_client("g560", env.project_id)
    secret = result["secret"]
    assert protocol.valid_secret(secret)
    stored = env.service.store.get_client("g560")
    assert stored is not None
    assert secret not in json.dumps(stored)  # plaintext never at rest
    assert stored["verifier"] == protocol.derive_verifier(
        secret, stored["salt"])
    with pytest.raises(RequestError):
        env.service.register_client("g560", env.project_id)  # duplicate


def test_list_clients_has_no_secret_material(env):
    env.service.register_client("g560", env.project_id, name="Lenovo")
    clients = env.service.list_clients()
    assert len(clients) == 1
    payload = json.dumps(clients)
    assert "verifier" not in payload and "salt" not in payload
    assert clients[0]["client_id"] == "g560"
    assert clients[0]["status"] == "active"


def test_rotation_and_revocation(env):
    first = env.service.register_client("g560", env.project_id)
    rotated = env.service.rotate_client_secret("g560")
    assert rotated["secret"] != first["secret"]
    env.service.revoke_client("g560")
    assert env.service.store.get_client("g560")["status"] == "revoked"
    with pytest.raises(AuthError):
        env.service.handshake("g560", protocol.new_nonce(), "x")


# -- handshake -----------------------------------------------------------------------

def test_handshake_round_trip(env):
    secret = env.register()
    key, payload = _handshake(env, secret)
    assert payload["client_id"] == "g560"
    assert payload["session_expires_at"] > time.time()
    assert payload["server_info"]["server"] == "forge-server"


def test_handshake_wrong_proof_fails_closed(env):
    env.register()
    nonce = protocol.new_nonce()
    challenge = env.http.post("/api/v1/link/challenge",
                              json={"client_id": "g560", "nonce": nonce}
                              ).json()
    proof = protocol.handshake_proof(
        protocol.derive_verifier("Z" * 43, challenge["salt"]),
        "g560", nonce, challenge["server_nonce"])
    response = env.http.post("/api/v1/link/handshake",
                             json={"client_id": "g560", "nonce": nonce,
                                   "proof": proof})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_FAILED"


def test_handshake_challenge_single_use(env):
    secret = env.register()
    nonce = protocol.new_nonce()
    challenge = env.http.post("/api/v1/link/challenge",
                              json={"client_id": "g560", "nonce": nonce}
                              ).json()
    verifier = protocol.derive_verifier(secret, challenge["salt"])
    proof = protocol.handshake_proof(verifier, "g560", nonce,
                                     challenge["server_nonce"])
    body = {"client_id": "g560", "nonce": nonce, "proof": proof}
    assert env.http.post("/api/v1/link/handshake",
                         json=body).status_code == 200
    # The same challenge replayed is dead: single use.
    replay = env.http.post("/api/v1/link/handshake", json=body)
    assert replay.status_code == 401


def test_unknown_client_gets_decoy_challenge(env):
    nonce = protocol.new_nonce()
    known = env.http.post("/api/v1/link/challenge",
                          json={"client_id": "g560", "nonce": nonce})
    unknown = env.http.post("/api/v1/link/challenge",
                            json={"client_id": "ghost", "nonce": nonce})
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json())
    # But the decoy never verifies.
    response = env.http.post("/api/v1/link/handshake",
                             json={"client_id": "ghost", "nonce": nonce,
                                   "proof": "0" * 64})
    assert response.status_code == 401


def test_new_handshake_supersedes_the_old_session(env):
    secret = env.register()
    key1, _ = _handshake(env, secret)
    key2, _ = _handshake(env, secret)
    path = "/api/v1/link/info"
    assert _signed_get(env, key1, path).status_code == 401  # old session dead
    assert _signed_get(env, key2, path).status_code == 200


# -- signed requests --------------------------------------------------------------------

def test_signed_requests_and_full_task_lifecycle(env):
    secret = env.register()
    key, _ = _handshake(env, secret)

    response = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality",
        "execution": "SERVER"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task"]["status"] in ("QUEUED", "RUNNING",
                                         "WAITING_APPROVAL")
    assert payload["effective_mode"] == "assisted"
    assert payload["execution"] == "SERVER"
    task_id = payload["task"]["id"]

    listing = _signed_get(env, key, "/api/v1/link/tasks")
    assert listing.status_code == 200
    assert any(t["id"] == task_id for t in listing.json()["tasks"])
    assert listing.json()["tasks"][0]["execution"] == "SERVER"

    state = _signed_get(env, key, "/api/v1/link/state")
    assert state.status_code == 200
    assert state.json()["queued"] >= 1

    info = _signed_get(env, key, "/api/v1/link/info")
    assert info.status_code == 200
    assert info.json()["client_id"] == "g560"
    assert info.json()["project_id"] == env.project_id

    logs = _signed_get(env, key, f"/api/v1/link/tasks/{task_id}/logs")
    assert logs.status_code == 200

    verification = _signed_get(
        env, key, f"/api/v1/link/tasks/{task_id}/verification")
    assert verification.status_code == 200


def test_replayed_nonce_rejected(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    path = "/api/v1/link/info"
    stamp = str(int(time.time()))
    first = _signed_get(env, key, path, timestamp=stamp, nonce="a" * 32)
    assert first.status_code == 200
    replay = _signed_get(env, key, path, timestamp=stamp, nonce="a" * 32)
    assert replay.status_code == 401
    # A fresh nonce at the same timestamp is fine again.
    assert _signed_get(env, key, path, timestamp=stamp,
                       nonce="c" * 32).status_code == 200


def test_tampered_body_rejected(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    path = "/api/v1/link/tasks"
    genuine = json.dumps({"requirement": "Add CSV export functionality"}).encode()
    timestamp = str(int(time.time()))
    nonce = protocol.new_nonce()
    signature = protocol.sign_request(key, "POST", path, genuine,
                                      timestamp, nonce)
    tampered = genuine.replace(b"CSV", b"ALL")
    response = _signed_post(env, key, path, {}, body=tampered,
                            timestamp=timestamp, nonce=nonce,
                            signature=signature)
    assert response.status_code == 401


def test_stale_timestamp_rejected(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    old = str(int(time.time()) - 10_000)
    response = _signed_get(env, key, "/api/v1/link/info", timestamp=old,
                           nonce="b" * 32)
    assert response.status_code == 401


def test_missing_headers_rejected(env):
    assert env.http.get("/api/v1/link/info").status_code == 401
    assert env.http.get("/api/v1/link/state").status_code == 401


def test_revoked_client_rejected_mid_session(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    assert _signed_get(env, key, "/api/v1/link/info").status_code == 200
    env.service.revoke_client("g560")
    assert _signed_get(env, key, "/api/v1/link/info").status_code == 401


# -- authoritative authorization ------------------------------------------------------------

def test_server_clamps_requested_mode_to_client_ceiling(env):
    secret = env.register("limited", max_mode="safe")
    key, _ = _handshake(env, secret, client_id="limited")

    requested = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality",
        "mode": "autonomous"}, client_id="limited")
    assert requested.status_code == 200
    # Client asked for autonomous; the server's ceiling (safe) wins.
    assert requested.json()["effective_mode"] == "safe"
    assert requested.json()["requested_mode"] == "autonomous"

    # Empty request -> the server's ceiling.
    defaulted = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality"},
        client_id="limited")
    assert defaulted.json()["effective_mode"] == "safe"


def test_client_can_tighten_but_never_loosen(env):
    secret = env.register("tight", max_mode="autonomous")
    key, _ = _handshake(env, secret, client_id="tight")
    response = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality",
        "mode": "safe"}, client_id="tight")
    assert response.json()["effective_mode"] == "safe"


def test_invalid_mode_rejected(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    response = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality", "mode": "godmode"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


# -- no command execution surface ------------------------------------------------------------

def test_no_endpoint_accepts_commands(env):
    """The link surface is a fixed vocabulary; there is no exec route."""
    secret = env.register()
    key, _ = _handshake(env, secret)
    for path in ("/api/v1/link/exec", "/api/v1/link/shell",
                 "/api/v1/link/run", "/api/v1/link/commands",
                 "/api/v1/link/compute/execute"):
        response = _signed_get(env, key, path)
        assert response.status_code == 404, path
        response = _signed_post(env, key, path, {"cmd": "id"})
        # 404 (no route) or 405 (static mount) — either way there is no
        # command-execution endpoint on the link surface.
        assert response.status_code in (404, 405), path


def test_oversized_link_body_rejected(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    path = "/api/v1/link/tasks"
    body = b'{"requirement": "' + b"x" * (256 * 1024 + 10) + b'"}'
    timestamp = str(int(time.time()))
    nonce = protocol.new_nonce()
    signature = protocol.sign_request(key, "POST", path, body, timestamp,
                                      nonce)
    response = env.http.post(path, content=body, headers={
        "Content-Type": "application/json",
        **protocol.signature_headers("g560", timestamp, nonce, signature)})
    assert response.status_code in (400, 413)


# -- approvals + mutations over the link ---------------------------------------------------------

def test_approval_round_trip_and_cancel(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    created = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality"})
    assert created.status_code == 200, created.text
    task_id = created.json()["task"]["id"]

    # Approve every write until the pipeline completes server-side.
    def finished():
        payload = _signed_get(env, key,
                              f"/api/v1/link/tasks/{task_id}").json()
        status = payload["task"]["status"]
        for approval in _signed_get(env, key,
                                    "/api/v1/link/approvals").json()[
                "approvals"]:
            decided = _signed_post(
                env, key,
                f"/api/v1/link/approvals/{approval['id']}/decide",
                {"approved": True})
            assert decided.status_code == 200, decided.text
        return payload["task"] if status in ("SUCCEEDED", "FAILED") else None

    final = wait_until(finished, timeout=60)
    assert final["status"] == "SUCCEEDED"
    assert final["execution"] == "SERVER"

    events = _signed_get(env, key,
                         f"/api/v1/link/tasks/{task_id}/events?after=0")
    assert events.status_code == 200
    assert events.json()["cursor"] > 0

    # Cancel a fresh task over the link (cooperative, prompt).
    second = _signed_post(env, key, "/api/v1/link/tasks", {
        "requirement": "Add CSV export functionality"})
    second_id = second.json()["task"]["id"]
    cancelled = _signed_post(env, key,
                             f"/api/v1/link/tasks/{second_id}/cancel", {})
    assert cancelled.status_code == 200, cancelled.text

    def cancelled_terminal():
        payload = _signed_get(env, key,
                              f"/api/v1/link/tasks/{second_id}").json()
        return payload["task"] if payload["task"]["status"] in (
            "CANCELLED", "FAILED", "ROLLED_BACK") else None

    assert wait_until(cancelled_terminal, timeout=30)["status"] == "CANCELLED"


def test_task_not_found_is_404(env):
    secret = env.register()
    key, _ = _handshake(env, secret)
    response = _signed_get(env, key, "/api/v1/link/tasks/t-doesnotexist")
    assert response.status_code == 404
    logs = _signed_get(env, key, "/api/v1/link/tasks/t-doesnotexist/logs")
    assert logs.status_code == 404
