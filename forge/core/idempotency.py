"""Deterministic idempotency key helper."""
import hashlib
def key(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
