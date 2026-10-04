"""Opaque task identifier generation."""
import uuid
def new_task_id(): return uuid.uuid4().hex
