"""Provider selection facade preserving provider truth."""
def healthy(provider):
    if provider is None:return False
    result=provider.health() if hasattr(provider,"health") else None
    return bool(result and result.get("healthy",result.get("configured",False)))
