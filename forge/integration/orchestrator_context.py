"""Orchestrator context assembled from verified pipeline inputs."""
from dataclasses import dataclass,field
@dataclass(frozen=True)
class OrchestratorContext:
    task_id:str
    project_id:str
    session_id:str
    capabilities:tuple=field(default_factory=tuple)
    quality_bar:str="standard"
