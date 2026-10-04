"""Agent lifecycle states."""
from enum import Enum
class AgentStatus(str,Enum):
    CREATED="created"; READY="ready"; RUNNING="running"; FAILED="failed"; RETIRED="retired"
