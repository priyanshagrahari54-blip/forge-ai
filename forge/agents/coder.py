from __future__ import annotations
import json, re
from typing import Any
from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolResult, ToolRuntime
from forge.security.permissions import PermissionManager
from forge.models.router import ModelRouter

class CoderAgent(AgentExecutor):
    name = "coder"
    def __init__(self, runtime: ToolRuntime | None = None, root: str = ".", router: ModelRouter | None = None):
        self.root, self.runtime, self.router = root, runtime or create_default_runtime(PermissionManager(), root), router or ModelRouter([__import__('forge.models.router', fromlist=['ModelInfo']).ModelInfo('local', 'coding', available=True, free=True, provider=__import__('forge.models.provider', fromlist=['LocalModelProvider']).LocalModelProvider(), capabilities=('coding', 'debugging', 'review'))])
    def describe(self) -> str: return "Responsible for implementing software changes using a routed model."
    def build_context(self, intelligence: RepositoryIntelligence, task: str, target_files: tuple[str,...]=(), target_symbols: tuple[str,...]=(), max_tokens: int=4000) -> AgentContext:
        return AgentContextBuilder(intelligence, max_tokens).build(task, target_files, target_symbols)
    def write_file(self, path: str, content: str, approved: bool = False) -> ToolResult:
        return self.runtime.execute("write_file", approved=approved, path=path, content=content)
    def _prompt(self, request: AgentRequest) -> str:
        context_parts = []
        for item in (request.context.items if request.context else []):
            try:
                from pathlib import Path
                content = (Path(self.root) / item.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = "<unavailable>"
            context_parts.append(f"FILE: {item.path}\n{content}")
        context = "\n\n".join(context_parts)
        return ("You are an autonomous coding agent. Implement the task, preserving architecture. "
                "Return ONLY JSON: {changes:{relative/path:str file contents}, explanation:str}. Never edit outside the repository.\n"
                f"TASK: {request.task.description}\nCONTEXT:\n{context}\nTOOLS/INSTRUCTIONS:{request.instructions}")
    @staticmethod
    def _changes(text: str) -> dict[str,str]:
        text = text.strip()
        if "```" in text: text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
        try: data = json.loads(text)
        except json.JSONDecodeError as exc: raise ValueError(f"Model returned invalid JSON: {exc}")
        changes = data.get("changes", data) if isinstance(data, dict) else {}
        if not isinstance(changes, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in changes.items()): raise ValueError("Model response changes must be a string mapping")
        return changes
    def execute(self, request: AgentRequest) -> AgentResponse:
        approved = bool(request.metadata.get("approved", False))
        try:
            model = self.router.select("coding", context_size=request.context.estimated_tokens if request.context else 0)
            if not model or not model.provider: return AgentResponse(False, error="No available coding model provider", agent=self.name, stage=request.stage)
            try:
                result = model.provider.generate(self._prompt(request), context=str(request.context), task=request.task.description)
            except Exception:
                self.router.record(model.name, False, None)
                raise
            changes = self._changes(result.text)
            if not changes: return AgentResponse(False, error="Model proposed no changes", agent=self.name, stage=request.stage)
            applied=[]
            for path, content in changes.items():
                res=self.write_file(path, content, approved=approved)
                if not res.success: return AgentResponse(False, error=f"Failed to write {path}: {res.error}", agent=self.name, stage=request.stage, metadata={"files": applied})
                applied.append(path)
            self.router.record(model.name, True, result.latency)
            return AgentResponse(True, output=result.text, agent=self.name, stage=request.stage, metadata={"files": applied, "model": model.name})
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage)
