"""Project/session workspace boundary facade."""
def open_scope(manager,project_id,session_id):
    if manager is None:return None
    return manager.get(project_id,session_id) if hasattr(manager,"get") else None
