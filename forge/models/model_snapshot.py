"""Stable read-only model routing snapshot."""
def snapshot(models):
    return [m.to_dict() if hasattr(m,"to_dict") else m for m in (models or ())]
