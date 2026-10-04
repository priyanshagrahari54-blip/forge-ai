"""Artifact policy boundary for generated outputs."""
def allowed(path,protected_prefixes=()):
    return not any(str(path).replace("\\","/").startswith(str(p).replace("\\","/")) for p in protected_prefixes)
