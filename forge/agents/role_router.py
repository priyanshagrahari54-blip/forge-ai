"""Deterministic specialist role routing facade."""
ROLE_CAPABILITIES={"researcher":{"research"},"engineer":{"software","ai","game"},"creative":{"creative"},"systems":{"os"},"tester":{"testing"},"security":{"security"}}
def route_roles(capabilities):
    wanted=set(capabilities or [])
    return sorted(role for role,caps in ROLE_CAPABILITIES.items() if caps & wanted)
