"""Small explicit state-transition helper."""
def transition(current,target,allowed):
    if target not in set(allowed.get(current,()) if isinstance(allowed,dict) else ()): return None
    return target
