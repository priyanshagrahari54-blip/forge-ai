from forge.runtime.runtime import ToolDefinition, ToolResult, ToolRuntime
from forge.security.audit import AuditLog
from forge.security.secrets import SecretRedactor, SecretScanner
from forge.tools.filesystem import FileSystemTool
from forge.tools.terminal import TerminalTool
from forge.tools.search import SearchTool
from forge.tools.git import GitTool


def create_default_runtime(
    permission_manager,
    root: str = ".",
    audit_log: AuditLog | None = None,
    redactor: SecretRedactor | None = None,
) -> ToolRuntime:
    runtime = ToolRuntime(
        permission_manager,
        audit_log=audit_log,
        redactor=redactor,
    )

    filesystem = FileSystemTool(root)
    terminal = TerminalTool(root)
    search = SearchTool(root)
    git = GitTool(root)

    runtime.register(
        ToolDefinition(
            name="read_file",
            description="Read a project file.",
            handler=lambda path: ToolResult.ok(
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
                or ToolResult.ok("write_file")
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
            handler=lambda query: ToolResult.ok(
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
            handler=lambda: ToolResult.ok(
                "git_status",
                git.status(),
            ),
            permission="git_status",
        )
    )

    scanner = SecretScanner()

    def scan_secrets(path: str) -> ToolResult:
        content = filesystem.read(path)
        findings = scanner.scan(content)

        return ToolResult.ok(
            "scan_secrets",
            metadata={
                "path": path,
                "findings": [
                    {
                        "type": finding.type.value,
                        "line": finding.line,
                        "column": finding.column,
                        "snippet": finding.snippet,
                        "confidence": finding.confidence,
                    }
                    for finding in findings
                ],
            },
        )

    runtime.register(
        ToolDefinition(
            name="scan_secrets",
            description="Scan a project file for exposed secrets.",
            handler=scan_secrets,
            permission="read_file",
        )
    )

    return runtime
