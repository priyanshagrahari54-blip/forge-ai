"""Explicit rollback marker; no rollback is claimed without evidence."""
from dataclasses import dataclass
@dataclass(frozen=True)
class RollbackMarker:
    checkpoint_id:str
    reason:str
    applied:bool=False
