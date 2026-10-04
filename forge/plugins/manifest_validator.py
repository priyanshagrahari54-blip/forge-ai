"""Fail-closed plugin manifest validation."""
def validate_manifest(manifest):
    required=("name","version","entrypoint")
    if any(not getattr(manifest,k,None) for k in required):return False
    if not getattr(manifest,"verified",False):return False
    return bool(getattr(manifest,"permissions",None) is not None)
