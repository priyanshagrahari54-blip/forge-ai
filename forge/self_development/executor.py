from __future__ import annotations
import json,time
from pathlib import Path
from typing import Callable, Optional
from forge.agents.coder import CoderAgent
from forge.agents.debugger import TestDebugLoop
from forge.core.task_engine import TaskEngine
from forge.core.supervisor import Supervisor
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.router import ModelRouter
from forge.self_development.evaluator import CandidateEvaluator, EvaluationResult
from forge.self_development.improvements import ImprovementCandidate
from forge.security.verification import VerificationPipeline
from forge.tools.checkpoint import CheckpointManager
from forge.tools.git import GitTool

class SelfDevelopmentExecutor:
    def __init__(self, root=".", supervisor=None, task_engine=None, registry=None, router=None, permissions=None, memory=None, git_tool=None):
        self.root=Path(root).resolve(); self.supervisor=supervisor or Supervisor("forge-self"); self.task_engine=task_engine or TaskEngine(); self.router=router or ModelRouter(); self.evaluator=CandidateEvaluator(self.root); self.git_tool=git_tool or GitTool(str(self.root)); self.checkpoints=CheckpointManager(self.root); self.coder=CoderAgent(root=str(self.root),router=self.router); self.verifier=VerificationPipeline(self.root)
    def execute_candidate(self, candidate: ImprovementCandidate, modifier_fn: Optional[Callable]=None) -> EvaluationResult:
        started=time.perf_counter(); baseline=self.evaluator.capture_state(); checkpoint=self.checkpoints.create(candidate.id)
        task=self.task_engine.add(f"self-{candidate.id}-{int(time.time()*1000)}",f"Self-improvement: {candidate.title}")
        task.status=task.status.CODING
        code_error=""
        try:
            if modifier_fn is not None: # backwards-compatible test hook, never required by autonomous path
                modifier_fn(candidate)
            else:
                intel=RepositoryIntelligence.build(self.root)
                context=self.coder.build_context(intel,candidate.proposed_improvement,tuple(candidate.affected_files))
                from forge.agents.execution import AgentRequest
                response=self.coder.execute(AgentRequest(task=task,stage=task.status,context=context,instructions=candidate.proposed_improvement,metadata={"approved":True}))
                if not response.success: code_error=response.error
            if code_error: raise RuntimeError(code_error)
            debug=TestDebugLoop(self.root, max_retries=3, debugger=__import__('forge.agents.debugger',fromlist=['DebuggerAgent']).DebuggerAgent(str(self.root),router=self.router))
            debug_result=debug.run(candidate.proposed_improvement, approved=True)
            state=self.evaluator.capture_state(); diff=self.git_tool.diff()+"\n"+self.git_tool.status(); verification=self.verifier.run(diff)
            if not debug_result.success or not verification.passed:
                state["passed_benchmarks"]=0
                state["verification_failures"]=[g.details for g in verification.failures]
            result=self.evaluator.evaluate(baseline,state)
            if result.accepted:
                files=self.git_tool.changed_files(); files=[f for f in files if not f.startswith(".forge/")]
                if files:
                    commit=self.git_tool.commit_files(files,f"self-dev: {candidate.id} - {candidate.title}")
                    if commit.returncode: result.accepted=False; result.rejection_reason=commit.stderr.strip()
            if not result.accepted: self.checkpoints.rollback(checkpoint, list(set(self.git_tool.changed_files()) | set(candidate.affected_files)))
            else: self.checkpoints.cleanup(checkpoint)
        except Exception as exc:
            self.checkpoints.rollback(checkpoint, list(set(self.git_tool.changed_files()) | set(candidate.affected_files)))
            result=EvaluationResult(accepted=False,rejection_reason=str(exc))
        record={"timestamp":time.time(),"candidate":candidate.to_dict(),"accepted":result.accepted,"rejection_reason":result.rejection_reason,"duration":time.perf_counter()-started,"status":self.supervisor.current_stage}
        history=self.root/".forge/self/history"; history.mkdir(parents=True,exist_ok=True); (history/f"run_{candidate.id}_{int(time.time()*1000)}.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
        return result
