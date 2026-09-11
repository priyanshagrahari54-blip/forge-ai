from __future__ import annotations

from forge.runtime.runtime import ToolDefinition
from forge.tools.filesystem import FileSystemTool
from forge.tools.terminal import TerminalTool
from forge.tools.search import SearchTool
from forge.tools.git import GitTool


def _is_pytest_command(command) -> bool:
    """True only for a constrained project-pytest invocation.

    The ``run_tests`` tool executes without write approval, so it accepts
    nothing but the exact current interpreter running ``-m pytest`` with safe
    flags and repository-relative paths. No shell, no other binaries, no
    interpreter escapes.
    """
    import sys
    from pathlib import PurePosixPath

    if not isinstance(command, list) or len(command) < 4:
        return False
    if command[0] != sys.executable:
        return False
    try:
        module_index = next(
            index for index, arg in enumerate(command)
            if arg == "-m" and command[index + 1] == "pytest")
    except (StopIteration, IndexError):
        return False
    if module_index < 1:
        return False
    for arg in command[1:module_index]:
        if arg not in ("-B", "-I", "-E"):
            return False
    for arg in command[module_index + 2:]:
        if arg in ("-q", "-p", "no:cacheprovider"):
            continue
        if not isinstance(arg, str) or not arg or arg.startswith("-"):
            return False
        candidate = PurePosixPath(arg)
        if (candidate.is_absolute() or ".." in candidate.parts
                or "\\" in arg or ".git" in candidate.parts
                or ".forge" in candidate.parts):
            return False
    return True


def create_default_runtime(permission_manager, root: str = "."):
    from forge.runtime.runtime import ToolRuntime

    runtime = ToolRuntime(permission_manager)

    filesystem = FileSystemTool(root)
    terminal = TerminalTool(root)
    search = SearchTool(root)
    git = GitTool(root)

    runtime.register(
        ToolDefinition(
            name="read_file",
            description="Read a project file.",
            handler=lambda path: __import__(
                "forge.runtime.runtime",
                fromlist=["ToolResult"],
            ).ToolResult.ok(
                "read_file",
                filesystem.read(path),
            ),
            permission="read_file",
        )
    )

    runtime.register(
        ToolDefinition(
            name="write_file",
            description="Write a project file.",
            handler=lambda path, content: (
                filesystem.write(path, content)
                or __import__(
                    "forge.runtime.runtime",
                    fromlist=["ToolResult"],
                ).ToolResult.ok("write_file")
            ),
            permission="write_file",
        )
    )

    runtime.register(
        ToolDefinition(
            name="delete_file",
            description="Delete a project file (explicit approval only).",
            handler=lambda path: (
                filesystem.delete(path)
                or __import__(
                    "forge.runtime.runtime",
                    fromlist=["ToolResult"],
                ).ToolResult.ok("delete_file")
            ),
            permission="delete_file",
        )
    )

    runtime.register(
        ToolDefinition(
            name="terminal",
            description="Run an approved terminal command.",
            handler=terminal.run,
            permission="run_command",
        )
    )

    def run_tests_handler(command: list[str]):
        from forge.runtime.runtime import ToolResult

        if not _is_pytest_command(command):
            return ToolResult.fail(
                "run_tests",
                "Only the project pytest suite may run without write approval.",
            )
        return terminal.run(command)

    runtime.register(
        ToolDefinition(
            name="run_tests",
            description="Run the project pytest suite (constrained).",
            handler=run_tests_handler,
            permission="run_tests",
        )
    )

    runtime.register(
        ToolDefinition(
            name="search",
            description="Search project text.",
            handler=lambda query: __import__(
                "forge.runtime.runtime",
                fromlist=["ToolResult"],
            ).ToolResult.ok(
                "search",
                metadata={
                    "results": search.text(query),
                },
            ),
        )
    )

    runtime.register(
        ToolDefinition(
            name="git_status",
            description="Inspect Git working tree status.",
            handler=lambda: __import__(
                "forge.runtime.runtime",
                fromlist=["ToolResult"],
            ).ToolResult.ok(
                "git_status",
                git.status(),
            ),
            permission="git_status",
        )
    )

    runtime.register(
        ToolDefinition(
            name="git_diff",
            description="Inspect unstaged Git working tree changes.",
            handler=lambda: __import__(
                "forge.runtime.runtime",
                fromlist=["ToolResult"],
            ).ToolResult.ok(
                "git_diff",
                git.diff(),
            ),
            permission="git_diff",
        )
    )

    return runtime
