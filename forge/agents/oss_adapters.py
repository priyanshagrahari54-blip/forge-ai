"""Catalog of open-source coding-agent runtimes Forge can delegate to.

These entries describe real projects. ``catalogued`` never means installed or
running; an adapter becomes live only after Forge verifies the executable,
configuration, permissions, and health at runtime.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OSSAgentAdapter:
    name: str
    repository: str
    website: str
    execution_style: str
    capabilities: tuple[str, ...]
    status: str = "catalogued"


OSS_AGENT_ADAPTERS: tuple[OSSAgentAdapter, ...] = (
    OSSAgentAdapter("OpenHands", "All-Hands-AI/OpenHands", "https://github.com/All-Hands-AI/OpenHands", "agent-runtime", ("coding", "reasoning", "tool_use", "computer_use")),
    OSSAgentAdapter("SWE-agent", "SWE-agent/SWE-agent", "https://github.com/SWE-agent/SWE-agent", "software-engineering-agent", ("coding", "debugging", "testing", "tool_use")),
    OSSAgentAdapter("Aider", "Aider-AI/aider", "https://github.com/Aider-AI/aider", "terminal-pair-programmer", ("coding", "tool_use", "review")),
    OSSAgentAdapter("Goose", "block/goose", "https://github.com/block/goose", "agent-framework", ("coding", "reasoning", "tool_use")),
    OSSAgentAdapter("OpenCode", "anomalyco/opencode", "https://github.com/anomalyco/opencode", "terminal-coding-agent", ("coding", "tool_use", "reasoning")),
    OSSAgentAdapter("Cline", "cline/cline", "https://github.com/cline/cline", "editor-agent", ("coding", "computer_use", "tool_use")),
)


def agent_adapter_catalog() -> list[dict[str, object]]:
    return [asdict(item) for item in OSS_AGENT_ADAPTERS]
