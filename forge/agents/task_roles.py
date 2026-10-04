"""Capability-driven specialist role mapping."""
ROLE_MAP={"research":"researcher","creative":"creative","game":"engineer","os":"systems","software":"engineer","ai":"engineer"}
def roles_for_capabilities(capabilities):
    return sorted({ROLE_MAP[c] for c in (capabilities or []) if c in ROLE_MAP})
