"""Delivery is allowed only after explicit acceptance PASS."""
def deliver(status, gate):
    return bool(gate(status)) if callable(gate) else False
