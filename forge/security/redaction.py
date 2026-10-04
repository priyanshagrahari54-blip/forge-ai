"""Recursive secret redaction for reports and telemetry."""
SECRET_KEYS={"token","secret","password","api_key","authorization"}
def redact(value):
    if isinstance(value,dict): return {k:("[REDACTED]" if k.lower() in SECRET_KEYS else redact(v)) for k,v in value.items()}
    if isinstance(value,list): return [redact(v) for v in value]
    return value
