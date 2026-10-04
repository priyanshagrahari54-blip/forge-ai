"""Browser actions require explicit scope and approval for risky operations."""
def allowed(action,approved=False):
    risk=str(getattr(action,"risk","low")).lower()
    return bool(getattr(action,"project_id","") and getattr(action,"session_id","") and (risk not in {"high","critical"} or approved))
