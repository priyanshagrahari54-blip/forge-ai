"""Browser action facade requiring policy approval."""
def execute(provider,action,approved=False):
    if provider is None or not provider.allowed(action,approved): return {"status":"BLOCKED"}
    return provider.execute(action)
