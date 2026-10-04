"""Basic path policy boundary; callers must still use canonical containment checks."""
def allowed(path,root):
    try:
        import os
        return os.path.commonpath([os.path.abspath(path),os.path.abspath(root)])==os.path.abspath(root)
    except (ValueError,OSError): return False
