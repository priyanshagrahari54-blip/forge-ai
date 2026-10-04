"""Fail-closed tool policy helper."""
def permitted(tool,permissions=()):
    if not getattr(tool,"verified",False): return False
    required=set(getattr(tool,"permissions",()) or ())
    return required.issubset(set(permissions or ()))
