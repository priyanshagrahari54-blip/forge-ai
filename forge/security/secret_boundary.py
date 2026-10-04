"""Prevent secrets from crossing ordinary artifact/report boundaries."""
def redact(value, keys=("token","secret","password","api_key")):
    if not isinstance(value,dict): return value
    return {k:("[REDACTED]" if any(x in k.lower() for x in keys) else v) for k,v in value.items()}
