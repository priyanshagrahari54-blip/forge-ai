"""Opaque run identifier generation."""
import uuid
def new_run_id(): return uuid.uuid4().hex
