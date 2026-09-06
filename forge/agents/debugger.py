from __future__ import annotations
import json, sys
from dataclasses import dataclass, field
from pathlib import Path
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.models.router import ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolRuntime
from forge.security.permissions import PermissionManager

@dataclass
class DebugAttempt:
    attempt_number:int; failure_error:str; diagnosis:str; modifications:dict[str,str]; test_passed:bool; test_output:str
@dataclass
class DebugLoopResult:
    success:bool; attempts:list[DebugAttempt]=field(default_factory=list); final_state:str=""; error:str=""

class DebuggerAgent(AgentExecutor):
    name="debugger"
    def __init__(self, root: str=".", runtime: ToolRuntime|None=None, router: ModelRouter|None=None):
        self.root=root; self.runtime=runtime or create_default_runtime(PermissionManager(), root); self.router=router or ModelRouter()
    def describe(self): return "Diagnoses test failures and applies bounded model-generated fixes."
    def diagnose(self, failure_output: str, source_code: str="") -> str:
        return failure_output[-2000:] if failure_output else "Unknown test failure"
    def execute(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(True, output=self.diagnose(request.instructions or request.metadata.get("error", "")), agent=self.name, stage=request.stage)
    def repair(self, task: str, failure: str, context: str="", approved: bool=True) -> dict[str,str]:
        model=self.router.select("debugging") or self.router.select("coding")
        if not model or not model.provider: raise RuntimeError("No debugging model available")
        prompt=("Diagnose and fix this test failure. Return ONLY JSON {changes:{relative/path:file contents}, explanation:str}. "
                "Make the smallest safe fix; do not modify tests to hide failures.\nTASK:"+task+"\nFAILURE:"+failure+"\nCONTEXT:"+context)
        result=model.provider.generate(prompt, context=context, task=task)
        data=json.loads(result.text.strip().replace("```json","").replace("```","") )
        changes=data.get("changes",{})
        if not isinstance(changes,dict): raise ValueError("Debugger model returned invalid changes")
        for path,content in changes.items():
            res=self.runtime.execute("write_file", approved=approved, path=path, content=content)
            if not res.success: raise RuntimeError(res.error or "write failed")
        self.router.record(model.name, True, result.latency)
        return changes

class TestDebugLoop:
    def __init__(self, root: str|Path=".", max_retries:int=3, debugger: DebuggerAgent|None=None, command:list[str]|None=None):
        self.root=str(root); self.max_retries=max(0,min(max_retries,10)); self.debugger=debugger or DebuggerAgent(self.root); self.command=command or [sys.executable,"-m","pytest","-q"]
    def run(self, task: str, context: str="", approved: bool=True) -> DebugLoopResult:
        attempts=[]
        for number in range(1,self.max_retries+2):
            result=self.debugger.runtime.execute("terminal", approved=approved, command=self.command)
            output=result.output or result.error or ""
            combined=(result.output or "") + result.metadata.get("stdout", "") + result.metadata.get("stderr", "")
            if result.success or (result.metadata.get("returncode") == 5 and ("no tests ran" in combined.lower() or "collected 0 items" in combined.lower())): return DebugLoopResult(True,attempts,"tests passed or no test suite")
            if number>self.max_retries: return DebugLoopResult(False,attempts,"tests failed",output)
            diagnosis=self.debugger.diagnose(output)
            try: changes=self.debugger.repair(task, output, context, approved)
            except Exception as exc: return DebugLoopResult(False,attempts,"repair failed",str(exc))
            attempts.append(DebugAttempt(number,output,diagnosis,changes,False,output))
        return DebugLoopResult(False,attempts,"tests failed")
