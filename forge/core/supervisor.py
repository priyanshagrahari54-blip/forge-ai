from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from forge.core.planner import Planner
from forge.core.state import ForgeState

class Supervisor:
    def __init__(self, project_name: str, root: str|Path = ".") -> None:
        self.state=ForgeState(project_name); self.planner=Planner(); self.root=Path(root).resolve(); self.current_stage="IDLE"; self.stage_history=[]
    def create_plan(self, request: str): return self.planner.create_plan(request)
    def start(self, task: str) -> None: self.state.start_task(task)
    def complete(self) -> None: self.state.complete_task()
    def set_stage(self, stage: str, details: Optional[Dict[str, Any]]=None) -> None:
        self.current_stage=stage; self.stage_history.append({"stage":stage,"details":details or {}})
    def execute_self_development_stage(self, stage: str, stage_fn: Callable[[],Any], details: Optional[Dict[str,Any]]=None) -> Any:
        self.set_stage(stage,details); return stage_fn()
    def run(self, requirement: str, *, approved: bool=False) -> dict[str,Any]:
        """Run one guarded requirement through model coding and verification.

        Approval is explicit: callers cannot accidentally turn a read-only
        inspection into a write. The returned evidence is suitable for a UI.
        """
        from forge.agents.coder import CoderAgent
        from forge.agents.execution import AgentRequest
        from forge.core.task_engine import TaskEngine, TaskStatus
        from forge.intelligence.repository import RepositoryIntelligence
        from forge.security.verification import VerificationPipeline
        from forge.tools.checkpoint import CheckpointManager
        from forge.tools.git import GitTool
        if not approved: return {"accepted":False,"stage":"APPROVAL_REQUIRED","error":"Explicit write approval is required"}
        git=GitTool(self.root); checkpoint=CheckpointManager(self.root).create("supervisor")
        engine=TaskEngine(); task=engine.add("supervisor-task","".join(requirement)); task.status=TaskStatus.CODING; self.set_stage("CODE")
        coder=CoderAgent(root=str(self.root)); intel=RepositoryIntelligence.build(self.root); context=coder.build_context(intel, requirement)
        response=coder.execute(AgentRequest(task,TaskStatus.CODING,context=context, instructions=requirement,metadata={"approved":True}))
        if not response.success:
            checkpoint_manager=CheckpointManager(self.root); checkpoint_manager.rollback(checkpoint, response.metadata.get("files", []))
            return {"accepted":False,"stage":"CODE","error":response.error}
        self.set_stage("VERIFY"); verification=VerificationPipeline(self.root).run(git.diff()+git.status())
        if not verification.passed:
            CheckpointManager(self.root).rollback(checkpoint, response.metadata.get("files", []))
            return {"accepted":False,"stage":"VERIFY","gates":[asdict(g) for g in verification.gates]}
        files=list(response.metadata.get("files",[])); git.commit_files(files,"forge: "+requirement)
        CheckpointManager(self.root).cleanup(checkpoint); self.set_stage("COMPLETED")
        return {"accepted":True,"files":files,"model":response.metadata.get("model"),"gates":[asdict(g) for g in verification.gates]}
