"""Bounded autonomy admission facade."""
def admit(controller,active_tasks=0):
    return controller.next() if controller is not None and controller.can_start(active_tasks) else None
