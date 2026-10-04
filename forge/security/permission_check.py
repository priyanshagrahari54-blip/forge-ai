"""Least-privilege permission check."""
def allows(required,granted): return set(required or ()).issubset(set(granted or ()))
