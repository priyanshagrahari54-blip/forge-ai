"""Central fail-closed security decision facade."""
def authorize(action,scope,policy):
    if policy is None or scope is None:return False
    return bool(policy(action,scope))
