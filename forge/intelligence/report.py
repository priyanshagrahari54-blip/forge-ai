from forge.intelligence.analyzer import ProjectAnalysis


def generate_report(
    analysis: ProjectAnalysis,
) -> str:

    repository = analysis.repository

    lines = [
        "# Repository Intelligence Report",
        "",
        f"Root: {repository.root}",
        "",
        f"Files: {len(repository.files)}",
        f"Directories: {len(repository.directories)}",
        "",
        "## File Types",
        "",
    ]

    for extension, count in sorted(
        repository.extensions.items()
    ):
        extension_name = extension or "[no extension]"

        lines.append(
            f"- `{extension_name}`: {count}"
        )

    lines.extend(
        [
            "",
            "## Python Code",
            "",
        ]
    )

    for file in analysis.python_files:

        lines.append(
            f"### `{file.path}`"
        )

        if file.imports:
            lines.append("Imports:")

            for import_name in file.imports:
                lines.append(
                    f"- `{import_name}`"
                )

        if file.symbols:
            lines.append("Symbols:")

            for symbol in file.symbols:
                lines.append(
                    f"- `{symbol.kind}` "
                    f"`{symbol.name}` "
                    f"(line {symbol.line})"
                )

        lines.append("")

    return "\n".join(lines)
