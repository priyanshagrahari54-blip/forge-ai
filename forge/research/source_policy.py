"""Research source policy: metadata is not authority."""
def source_allowed(source,allowlist=()):
    if not source:return False
    return not allowlist or source in set(allowlist)
