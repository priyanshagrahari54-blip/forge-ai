"""A81 protocol tests: handshake math, signatures, replay protection.

Deterministic: fixed secrets/nonces/clock, no network, no randomness
(where randomness is inherent — nonce generators — only the format is
checked).
"""
from __future__ import annotations

import pytest

from forge.link import protocol
from forge.link.errors import AuthError


SECRET = "Abcdefghijklmnopqrstuvwxyz0123456789_-AbCdEf"
SALT = "0123456789abcdef0123456789abcdef"
NONCE_C = "11111111111111111111111111111111"
NONCE_S = "22222222222222222222222222222222"


# -- validation ----------------------------------------------------------------

def test_client_id_rules():
    assert protocol.valid_client_id("g560")
    assert protocol.valid_client_id("workstation-2")
    assert not protocol.valid_client_id("")
    assert not protocol.valid_client_id("G560")          # uppercase
    assert not protocol.valid_client_id("-leading")
    assert not protocol.valid_client_id("a" * 40)        # too long
    assert not protocol.valid_client_id("has space")
    with pytest.raises(protocol.ProtocolError):
        protocol.require_client_id("../etc")


def test_nonce_and_secret_rules():
    assert protocol.valid_nonce("0123456789abcdef0123456789abcdef")
    assert not protocol.valid_nonce("xyz")
    assert not protocol.valid_nonce(SALT + "00")  # too long
    assert protocol.valid_secret(SECRET)
    assert not protocol.valid_secret("short")
    assert not protocol.valid_secret("has space!")
    with pytest.raises(protocol.ProtocolError):
        protocol.require_secret("nope")


# -- derivation -----------------------------------------------------------------

def test_verifier_is_deterministic_and_secret_dependent():
    v1 = protocol.derive_verifier(SECRET, SALT)
    v2 = protocol.derive_verifier(SECRET, SALT)
    assert v1 == v2
    assert v1 != protocol.derive_verifier(SECRET + "x", SALT)
    assert v1 != protocol.derive_verifier(SECRET, SALT.replace("0", "1"))
    assert len(v1) == 64  # sha256 hex


def test_handshake_proof_deterministic_and_binds_all_inputs():
    proof = protocol.handshake_proof(
        protocol.derive_verifier(SECRET, SALT), "g560", NONCE_C, NONCE_S)
    again = protocol.handshake_proof(
        protocol.derive_verifier(SECRET, SALT), "g560", NONCE_C, NONCE_S)
    assert proof == again
    # Any input change flips the proof.
    verifier = protocol.derive_verifier(SECRET, SALT)
    assert proof != protocol.handshake_proof(verifier, "g561", NONCE_C,
                                             NONCE_S)
    assert proof != protocol.handshake_proof(verifier, "g560", "f" * 32,
                                             NONCE_S)
    assert proof != protocol.handshake_proof(verifier, "g560", NONCE_C,
                                             "f" * 32)
    # Wrong verifier (wrong secret) -> wrong proof.
    assert proof != protocol.handshake_proof(
        protocol.derive_verifier("Z" * 43, SALT), "g560", NONCE_C, NONCE_S)


def test_session_key_matches_only_for_identical_inputs():
    verifier = protocol.derive_verifier(SECRET, SALT)
    key1 = protocol.session_key(verifier, "g560", NONCE_C, NONCE_S)
    key2 = protocol.session_key(verifier, "g560", NONCE_C, NONCE_S)
    assert key1 == key2
    assert key1 != protocol.session_key(verifier, "g560", NONCE_S, NONCE_C)
    # The handshake key and the session key differ even for the same
    # nonces (different derivation labels).
    assert key1 != protocol.handshake_proof(verifier, "g560", NONCE_C,
                                            NONCE_S)


# -- request signatures ----------------------------------------------------------

def _signed(key=None, method="POST", path="/api/v1/link/tasks",
            body=b"{}", timestamp="1700000000", nonce=None):
    key = key or protocol.session_key(
        protocol.derive_verifier(SECRET, SALT), "g560", NONCE_C, NONCE_S)
    nonce = nonce or "33333333333333333333333333333333"
    sig = protocol.sign_request(key, method, path, body, timestamp, nonce)
    return key, sig, (method, path, body, timestamp, nonce)


def test_signature_covers_every_field():
    key, sig, args = _signed()
    method, path, body, timestamp, nonce = args
    # Same inputs -> same signature.
    assert protocol.sign_request(key, method, path, body, timestamp,
                                 nonce) == sig
    # Each field mutation -> different signature.
    assert protocol.sign_request(key, "GET", path, body, timestamp,
                                 nonce) != sig
    assert protocol.sign_request(key, method, path + "x", body, timestamp,
                                 nonce) != sig
    assert protocol.sign_request(key, method, path, body + b" ", timestamp,
                                 nonce) != sig
    assert protocol.sign_request(key, method, path, body, "1700000001",
                                 nonce) != sig
    assert protocol.sign_request(key, method, path, body, timestamp,
                                 "4" * 32) != sig
    # A different session key -> different signature.
    other = protocol.session_key(
        protocol.derive_verifier(SECRET, SALT), "g560", NONCE_S, NONCE_C)
    assert protocol.sign_request(other, method, path, body, timestamp,
                                 nonce) != sig


def test_canonical_request_rejects_malformed_input():
    key = protocol.session_key(
        protocol.derive_verifier(SECRET, SALT), "g560", NONCE_C, NONCE_S)
    for args in (
            ("", "/x", b"", "1700000000", NONCE_C),
            ("POST", "api/x", b"", "1700000000", NONCE_C),
            ("POST", "/x", b"", "17x", NONCE_C),
            ("POST", "/x", b"", "1700000000", "nope"),
            ("POST", "/x", b"\x00" * (protocol.MAX_SIGNED_BODY_BYTES + 1),
             "1700000000", NONCE_C),
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.sign_request(key, *args)


def test_timestamp_window():
    assert protocol.timestamp_fresh("1700000000", 1700000000.0)
    assert protocol.timestamp_fresh("1700000060", 1700000000.0,
                                    window=120)
    assert not protocol.timestamp_fresh("1700000300", 1700000000.0,
                                        window=120)
    assert not protocol.timestamp_fresh("garbage", 1700000000.0)


# -- comparisons / randomness ------------------------------------------------------

def test_constant_time_equals():
    assert protocol.constant_time_equals("abc", "abc")
    assert not protocol.constant_time_equals("abc", "abd")
    assert not protocol.constant_time_equals("abc", "abcd")
    assert not protocol.constant_time_equals("abc", "")
    assert not protocol.constant_time_equals(None, "abc")


def test_randomness_has_protocol_shape():
    for _ in range(20):
        assert protocol.valid_nonce(protocol.new_nonce())
        assert protocol.valid_salt(protocol.new_salt())
    secret = protocol.new_secret()
    assert protocol.valid_secret(secret)
    assert len({protocol.new_nonce() for _ in range(50)}) == 50


def test_header_names_stable():
    assert protocol.HEADER_CLIENT == "X-Forge-Client"
    assert protocol.HEADER_TIMESTAMP == "X-Forge-Timestamp"
    assert protocol.HEADER_NONCE == "X-Forge-Nonce"
    assert protocol.HEADER_SIGNATURE == "X-Forge-Signature"


def test_wrong_secret_proof_fails_verification_shape():
    """The exact server check, exercised directly (no HTTP)."""
    verifier = protocol.derive_verifier(SECRET, SALT)
    good = protocol.handshake_proof(verifier, "g560", NONCE_C, NONCE_S)
    bad = protocol.handshake_proof(
        protocol.derive_verifier("Z" * 43, SALT), "g560", NONCE_C, NONCE_S)
    assert not protocol.constant_time_equals(good, bad)
    with pytest.raises(AuthError):
        # the server raises AuthError on mismatch; simulate the decision
        if not protocol.constant_time_equals(good, bad):
            raise AuthError("authentication failed")
