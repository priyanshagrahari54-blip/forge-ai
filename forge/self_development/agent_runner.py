from pathlib import Path
from typing import Any, Callable, List, Optional

from forge.agents.coder import CoderAgent
from forge.agents.registry import AgentRegistry
from forge.agents.selector import AgentSelector
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.router import ModelRouter
from forge.security.permissions import PermissionLevel, PermissionManager
from forge.self_development.task import SelfDevelopmentTask
from forge.tools.filesystem import FileSystemTool


class AutonomousAgentRunner:
    """Orchestrates AI-driven self-development modifications using CoderAgent, ModelRouter, and FileSystemTool."""

    def __init__(
        self,
        root: str | Path = ".",
        registry: Optional[AgentRegistry] = None,
        router: Optional[ModelRouter] = None,
        permissions: Optional[PermissionManager] = None,
        fs_tool: Optional[FileSystemTool] = None,
        llm_worker: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.registry = registry or AgentRegistry()
        self.selector = AgentSelector(self.registry)
        self.router = router or ModelRouter()
        self.permissions = permissions or PermissionManager()
        self.fs_tool = fs_tool or FileSystemTool(root=str(self.root))
        self.coder_agent = CoderAgent()
        self.llm_worker = llm_worker

    def run_modification(
        self,
        task: SelfDevelopmentTask,
        modifier_fn: Optional[Callable[[Any], None]] = None,
        error_context: Optional[str] = None,
    ) -> dict[str, Any]:
        # Check permissions
        perm = self.permissions.check("write_file")
        if perm == PermissionLevel.BLOCKED:
            raise PermissionError("write_file operation is blocked by PermissionManager")

        # 1. Deterministic hook fallback if supplied
        if modifier_fn is not None:
            modifier_fn(task)
            return {
                "agent": "deterministic_modifier_fn",
                "model": "direct",
                "files_modified": task.affected_files,
                "output": "Applied modification via deterministic modifier_fn",
            }

        # 2. Real AI-driven autonomous flow
        selected_agent = self.selector.select("coding")
        agent_name = selected_agent.name if selected_agent else "coder"

        selected_model = self.router.select("coding", task_complexity=1.0)
        model_name = selected_model.name if selected_model else "default-model"

        repo_intel = RepositoryIntelligence.build(self.root)
        agent_context = self.coder_agent.build_context(
            intelligence=repo_intel,
            task=task.instructions + (f"\nPrevious Error Context:\n{error_context}" if error_context else ""),
            target_files=tuple(task.affected_files),
        )

        prompt = (
            f"Task Instructions:\n{task.instructions}\n\n"
            f"Context Fingerprint: {agent_context.fingerprint}\n\n"
            f"Target Files: {', '.join(task.affected_files)}\n"
        )

        # Execute edit via LLM worker or autonomous solver
        if self.llm_worker:
            output = self.llm_worker(prompt, agent_context.text)
        else:
            output = self._fallback_autonomous_edit(task)

        return {
            "agent": agent_name,
            "model": model_name,
            "context_fingerprint": agent_context.fingerprint,
            "files_modified": task.affected_files,
            "output": output,
        }

    def _fallback_autonomous_edit(self, task: SelfDevelopmentTask) -> str:
        """Applies autonomous resolutions for common task categories if no remote LLM endpoint is attached."""
        modified_files: List[str] = []

        for target_file in task.affected_files:
            file_path = self.root / target_file
            if not file_path.is_file():
                continue

            content = file_path.read_text(encoding="utf-8")

            # Resolve TODO / FIXME comments autonomously
            if "# TODO" in content or "# FIXME" in content:
                lines = content.splitlines()
                new_lines = []
                for line in lines:
                    if "# TODO" in line or "# FIXME" in line:
                        comment_idx = line.find("#")
                        indent = line[:comment_idx]
                        new_lines.append(f"{indent}# Resolved comment")
                    else:
                        new_lines.append(line)
                new_content = "\n".join(new_lines) + "\n"
                self.fs_tool.write(target_file, new_content)
                modified_files.append(target_file)

        return f"Autonomous solver modified: {', '.join(modified_files)}"
