from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.router import ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolResult, ToolRuntime
from forge.security.permissions import PermissionManager


_SECRET_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_MAX_FILE_BYTES = 2 * 1024 * 1024


class CoderAgent(AgentExecutor):
    name = "coder"

    def __init__(self, runtime: ToolRuntime | None = None, root: str = ".", router: ModelRouter | None = None,
                 fabric: "ModelFabric | None" = None):
        # Routing input priority: explicit fabric > explicit legacy router >
        # default fabric. The legacy router path is preserved verbatim so
        # pre-existing integrations keep their exact behavior.
        if fabric is not None:
            self.fabric = fabric
            self.router = None
        elif router is not None:
            self.fabric = None
            self.router = router
        else:
            from forge.models.fabric import ModelFabric
            self.fabric = ModelFabric.from_defaults()
            self.router = None
        self.root = str(Path(root).resolve())
        self.runtime = runtime or create_default_runtime(PermissionManager(), self.root)

    def describe(self) -> str:
        return "Responsible for implementing software changes using a routed model."

    def build_context(self, intelligence: RepositoryIntelligence, task: str,
                      target_files: tuple[str, ...] = (), target_symbols: tuple[str, ...] = (),
                      max_tokens: int = 4000) -> AgentContext:
        return AgentContextBuilder(intelligence, max_tokens).build(task, target_files, target_symbols)

    def write_file(self, path: str, content: str, approved: bool = False) -> ToolResult:
        return self.runtime.execute("write_file", approved=approved, path=path, content=content)

    def _prompt(self, request: AgentRequest) -> str:
        context_parts = []
        for item in (request.context.items if request.context else []):
            try:
                content = (Path(self.root) / item.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = "<unavailable>"
            context_parts.append(f"FILE: {item.path}\n{content}")
        context = "\n\n".join(context_parts)
        return (
            "You are an autonomous coding agent. Implement the task while preserving architecture. "
            "Return ONLY JSON: {changes:{relative/path:str file contents}, explanation:str}. "
            "Never edit outside the repository.\n"
            f"TASK: {request.task.description}\nCONTEXT:\n{context}\nTOOLS/INSTRUCTIONS:{request.instructions}"
        )

    def _changes(self, text: str) -> dict[str, str]:
        cleaned = text.strip()
        if "```" in cleaned:
            cleaned = re.sub(r"```(?:json)?", "", cleaned).replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model returned invalid JSON: {exc}") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("changes"), dict)
                or not isinstance(data.get("explanation"), str)):
            raise ValueError("Model response must contain changes mapping and explanation string")
        changes = data["changes"]
        if not all(isinstance(path, str) and isinstance(content, str)
                   for path, content in changes.items()):
            raise ValueError("Every model change must map a path to string file contents")

        validated: dict[str, str] = {}
        for path, content in changes.items():
            self._validate_path(path)
            encoded = content.encode("utf-8")
            if len(encoded) > _MAX_FILE_BYTES:
                raise ValueError(f"Model file exceeds {_MAX_FILE_BYTES} bytes: {path}")
            if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
                raise ValueError(f"Model response appears to contain a secret: {path}")
            # Encoding above is also an explicit UTF-8 validation.
            validated[path] = content
        return validated

    @staticmethod
    def _validate_path(path: str) -> None:
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", path):
            raise ValueError(f"Model path must be relative: {path!r}")
        if ".." in candidate.parts or ".git" in candidate.parts or ".forge" in candidate.parts:
            raise ValueError(f"Model path is outside the permitted source tree: {path!r}")
        if "\\" in path:
            raise ValueError(f"Model path must use repository-relative POSIX separators: {path!r}")

    def _execute_via_fabric(self, request: AgentRequest) -> AgentResponse:
        """Code through the centralized Model Fabric.

        The fabric routes, calls the provider, records telemetry/feedback, and
        returns a structured response. Every write still goes through the
        permissioned ToolRuntime and the same structural/path/secret validation
        as the legacy path; model output is never trusted or written directly.
        """
        from forge.models.request import ModelRequest

        model_request = ModelRequest(
            prompt=self._prompt(request),
            capability="coding",
            required_capabilities=("coding",),
            context=str(request.context) if request.context else "",
            task=request.task.description,
            min_context_window=request.context.estimated_tokens if request.context else 0,
            prefer_local=True,
            prefer_free=True,
        )
        response = self.fabric.generate(model_request)
        if not response.success:
            return AgentResponse(
                False,
                error=response.error or "No available coding model provider; configure Ollama or another provider",
                agent=self.name,
                stage=request.stage,
            )
        try:
            changes = self._changes(response.text)
            if not changes:
                raise ValueError("Model proposed no changes")
            applied: list[str] = []
            for path, content in changes.items():
                result = self.write_file(path, content, approved=bool(request.metadata.get("approved", False)))
                if not result.success:
                    return AgentResponse(False, error=f"Failed to write {path}: {result.error}", agent=self.name,
                                         stage=request.stage, metadata={"files": applied})
                applied.append(path)
            return AgentResponse(True, output=response.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": response.model,
                                           "provider": response.provider})
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage,
                                 metadata={"files": []})

    def execute(self, request: AgentRequest) -> AgentResponse:
        if self.fabric is not None:
            return self._execute_via_fabric(request)
        model = None
        try:
            model = self.router.select("coding", context_size=request.context.estimated_tokens if request.context else 0)
            if not model or not model.provider:
                return AgentResponse(False, error="No available coding model provider; configure Ollama or another provider", agent=self.name, stage=request.stage)
            try:
                result = model.provider.generate(self._prompt(request), context=str(request.context), task=request.task.description)
            except Exception:
                self.router.record(model.name, False, None, capability="coding", task_complexity=1.0)
                raise
            changes = self._changes(result.text)
            if not changes:
                raise ValueError("Model proposed no changes")
            applied: list[str] = []
            for path, content in changes.items():
                response = self.write_file(path, content, approved=bool(request.metadata.get("approved", False)))
                if not response.success:
                    return AgentResponse(False, error=f"Failed to write {path}: {response.error}", agent=self.name,
                                         stage=request.stage, metadata={"files": applied})
                applied.append(path)
            self.router.record(model.name, True, result.latency, capability="coding", task_complexity=1.0)
            return AgentResponse(True, output=result.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": model.name})
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage,
                                 metadata={"files": []})
