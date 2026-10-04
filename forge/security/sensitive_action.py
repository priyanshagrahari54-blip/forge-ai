"""Sensitive action classification."""
SENSITIVE={"delete","publish","deploy","install","execute_remote","write_protected"}
def is_sensitive(action): return str(action).lower() in SENSITIVE
