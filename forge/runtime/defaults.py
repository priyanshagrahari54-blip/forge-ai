from forge.runtime.runtime import ToolDefinition
from forge.tools.filesystem import FileSystemTool
from forge.tools.terminal import TerminalTool
from forge.tools.search import SearchTool
from forge.tools.git import GitTool


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
            name="terminal",
            description="Run an approved terminal command.",
            handler=terminal.run,
            permission="run_command",
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

    return runtime
