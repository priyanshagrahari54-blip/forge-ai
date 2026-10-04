"""Repair loop facade; retry only when policy permits."""
def repair(failure,policy,handler):
    attempt=getattr(failure,"attempt",1)
    if policy is None or not policy.allowed(attempt) or not callable(handler): return None
    return handler(failure)
