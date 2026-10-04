"""Stable read-only agent roster snapshot."""
def snapshot(agents):
    return [a.to_dict() if hasattr(a,"to_dict") else a for a in (agents or ())]
