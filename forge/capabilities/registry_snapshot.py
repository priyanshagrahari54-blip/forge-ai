"""Stable read-only capability registry snapshot."""
def snapshot(registry):
    return [c.to_dict() if hasattr(c,"to_dict") else c for c in registry.all()] if registry is not None else []
