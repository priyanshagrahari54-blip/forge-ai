"""Model lifecycle states."""
from enum import Enum
class ModelStatus(str,Enum):
    DISCOVERED="discovered"; READY="ready"; UNAVAILABLE="unavailable"; BLOCKED="blocked"
