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
from forge.tools.change_applier import ChangeApplier, CodeChange


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
        # Every model-produced write goes through the controlled change-application
        # layer (path/content/secret validation + permissioned ToolRuntime).
        self.applier = ChangeApplier(self.runtime)

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
            # Defense in depth: context files must stay inside the repository.
            if not self._is_repo_relative(item.path):
                continue
            try:
                content = (Path(self.root) / item.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = "<unavailable>"
            context_parts.append(f"FILE: {item.path}\n{content}")
        context = "\n\n".join(context_parts)
        return (
            "You are an autonomous coding agent. Implement the task while preserving architecture. "
            "Return ONLY JSON with either "
            "{changes:{relative/path:str file contents}, explanation:str} or "
            "{summary:str, changes:[{path:str, action:create|modify, content:str}], "
            "tests:[relative/path], reasoning_summary:str, risks:[str]}. "
            "Never edit outside the repository, never emit secrets, and never include hidden reasoning.\n"
            f"TASK: {request.task.description}\nCONTEXT:\n{context}\nTOOLS/INSTRUCTIONS:{request.instructions}"
        )

    @staticmethod
    def _is_repo_relative(path: str) -> bool:
        candidate = PurePosixPath(path)
        return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts

    def _parse_changes(self, text: str) -> tuple[dict[str, str], dict]:
        """Parse a model response into validated changes plus structured summary.

        Accepts the legacy mapping schema ``{"changes": {path: content}}`` and
        the richer list schema ``{"changes": [{path, action, content}], ...}``.
        Deletions are rejected: autonomous runs never delete files implicitly.
        """
        cleaned = text.strip()
        if "```" in cleaned:
            cleaned = re.sub(r"```(?:json)?", "", cleaned).replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Model response must be a JSON object")

        summary = data.get("summary", "")
        reasoning = data.get("reasoning_summary", "")
        risks = data.get("risks", [])
        tests = data.get("tests", [])

        changes_raw = data.get("changes")
        validated: dict[str, str] = {}
        if isinstance(changes_raw, dict):
            if not all(isinstance(path, str) and isinstance(content, str)
                       for path, content in changes_raw.items()):
                raise ValueError("Every model change must map a path to string file contents")
            for path, content in changes_raw.items():
                self._validate_change(path, content)
                validated[path] = content
        elif isinstance(changes_raw, list):
            for item in changes_raw:
                if not isinstance(item, dict):
                    raise ValueError("Every model change entry must be an object")
                path = item.get("path")
                content = item.get("content")
                action = item.get("action", "modify")
                if not isinstance(path, str) or not isinstance(content, str):
                    raise ValueError("Every model change entry must have string path and content")
                if action not in ("create", "modify"):
                    raise ValueError(f"Unsupported change action for {path!r}: {action!r}")
                self._validate_change(path, content)
                validated[path] = content
        else:
            raise ValueError("Model response must contain a changes mapping or list")

        extra = {
            "summary": summary if isinstance(summary, str) else "",
            "reasoning_summary": reasoning if isinstance(reasoning, str) else "",
            "risks": [str(r) for r in risks] if isinstance(risks, list) else [],
            "tests": [str(t) for t in tests] if isinstance(tests, list) else [],
        }
        return validated, extra

    def _validate_change(self, path: str, content: str) -> None:
        self._validate_path(path)
        self._validate_content(path, content)
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValueError(f"Model file exceeds {_MAX_FILE_BYTES} bytes: {path}")
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"Model response appears to contain a secret: {path}")

    def _changes(self, text: str) -> dict[str, str]:
        validated, _extra = self._parse_changes(text)
        return validated

    def _apply_changes(self, changes: dict[str, str], approved: bool) -> tuple[list[str], list[str]]:
        """Apply validated changes through the controlled change-application layer."""
        result = self.applier.apply(
            [CodeChange(path=path, content=content) for path, content in changes.items()],
            approved=approved,
            label="coder",
        )
        return result.changed_paths, result.errors

    @staticmethod
    def _validate_content(path: str, content: str) -> None:
        """Reject syntactically invalid Python before it can be written.

        ``compile`` parses without executing, so untrusted model output is
        syntax-checked safely. Non-Python files are validated structurally only.
        """
        if not path.endswith(".py"):
            return
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            raise ValueError(f"Model produced invalid Python for {path}: {exc}") from exc

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
            changes, extra = self._parse_changes(response.text)
            if not changes:
                raise ValueError("Model proposed no changes")
            approved = bool(request.metadata.get("approved", False))
            applied, errors = self._apply_changes(changes, approved)
            if errors:
                return AgentResponse(False, error=errors[0], agent=self.name,
                                     stage=request.stage, metadata={"files": applied})
            return AgentResponse(True, output=response.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": response.model,
                                           "provider": response.provider, **extra})
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
            changes, extra = self._parse_changes(result.text)
            if not changes:
                raise ValueError("Model proposed no changes")
            approved = bool(request.metadata.get("approved", False))
            applied, errors = self._apply_changes(changes, approved)
            if errors:
                return AgentResponse(False, error=errors[0], agent=self.name,
                                     stage=request.stage, metadata={"files": applied})
            self.router.record(model.name, True, result.latency, capability="coding", task_complexity=1.0)
            return AgentResponse(True, output=result.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": model.name, **extra})
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage,
                                 metadata={"files": []})
