"""Trust boundary helper for externally discovered artifacts."""
def trust_state(verified=False,security_status="unverified",license_status="unverified"):
    return "TRUSTED" if verified and security_status in {"verified","reviewed"} and license_status in {"verified","reviewed"} else "UNTRUSTED"
