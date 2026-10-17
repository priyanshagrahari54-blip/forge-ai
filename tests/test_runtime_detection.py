from pathlib import Path

from forge.intelligence.runtime_detection import (
    RuntimeDetector,
    generate_runtime_report,
)


def create_python_project(root: Path) -> None:
    (root / ".gitignore").write_text(
        "__pycache__/\n.venv/\n",
        encoding="utf-8",
    )

    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n",
        encoding="utf-8",
    )

    package = root / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )

    tests = root / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n",
        encoding="utf-8",
    )


def test_python_runtime_detection(tmp_path: Path):
    create_python_project(tmp_path)

    runtime = RuntimeDetector(tmp_path).detect()

    assert "python" in runtime.project_type

    assert runtime.has_command(
        "install",
        "python -m pip install -e .",
    )

    assert runtime.has_command(
        "test",
        "python -m pytest",
    )

    assert runtime.has_command(
        "run",
        "python -m demo",
    )


def test_docker_detection(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12\n",
        encoding="utf-8",
    )

    runtime = RuntimeDetector(tmp_path).detect()

    assert "docker" in runtime.project_type
    assert runtime.has_command(
        "build",
        "docker build .",
    )


def test_node_script_detection(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        """
{
  "name": "demo",
  "scripts": {
    "test": "vitest",
    "build": "vite build",
    "start": "node server.js",
    "dev": "vite"
  }
}
""",
        encoding="utf-8",
    )

    runtime = RuntimeDetector(tmp_path).detect()

    assert "node" in runtime.project_type
    assert runtime.has_command("test", "npm run test")
    assert runtime.has_command("build", "npm run build")
    assert runtime.has_command("run", "npm run start")
    assert runtime.has_command("dev", "npm run dev")


def test_gitignore_is_respected(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(
        "ignored/\n",
        encoding="utf-8",
    )

    ignored = tmp_path / "ignored"
    ignored.mkdir()

    (ignored / "package.json").write_text(
        '{"scripts":{"test":"fake"}}',
        encoding="utf-8",
    )

    runtime = RuntimeDetector(tmp_path).detect()

    assert "node" not in runtime.project_type


def test_runtime_report(tmp_path: Path):
    create_python_project(tmp_path)

    runtime = RuntimeDetector(tmp_path).detect()
    report = generate_runtime_report(runtime)

    assert "# Runtime Detection" in report
    assert "## Project Types" in report
    assert "## Commands" in report
    assert "`python`" in report
    assert "`python -m pytest`" in report


def test_common_language_ecosystems_are_detected_without_claiming_tools(tmp_path: Path):
    markers = {
        "main.rs": "fn main() {}",
        "go.mod": "module example.test/demo\n",
        "Main.java": "class Main {}",
        "lib.kt": "fun main() {}",
        "src/main.c": "int main(void) { return 0; }",
        "src/engine.cpp": "int main() { return 0; }",
        "App.cs": "class App {}",
        "index.php": "<?php echo 'ok';",
        "app.rb": "puts 'ok'",
        "main.swift": "print(\"ok\")",
        "main.dart": "void main() {}",
        "main.lua": "print('ok')",
        "script.ps1": "Write-Output ok",
        "schema.sql": "select 1;",
        "view.vue": "<template><div /></template>",
        "page.svelte": "<p>ok</p>",
        "deno.json": "{}",
    }
    for relative, content in markers.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    runtime = RuntimeDetector(tmp_path).detect()
    for expected in (
        "rust", "go", "java", "kotlin", "c", "cpp", "csharp",
        "php", "ruby", "swift", "dart", "lua", "powershell",
        "sql", "vue", "svelte", "deno",
    ):
        assert expected in runtime.project_type

    # Detection is evidence only: don't invent commands/tool availability.
    assert not runtime.commands_for("test")
    assert not runtime.commands_for("build")
