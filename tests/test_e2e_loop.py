from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from forge.core.supervisor import Supervisor
from forge.tools.git import GitTool


def setup_isolated_repo(repo_dir: Path) -> None:
    git = GitTool(str(repo_dir))
    git.init()
    git.run("config", "user.name", "Forge Test")
    git.run("config", "user.email", "test@forge.ai")

    # Initial codebase
    (repo_dir / "exporter.py").write_text(
        "def export_data(data):\n    return list(data)\n"
    )
    (repo_dir / "test_exporter.py").write_text(
        "from exporter import export_data\n\n"
        "def test_export_data():\n"
        "    assert export_data([1, 2]) == [1, 2]\n"
    )

    git.add(".")
    git.commit("Initial commit")


def test_e2e_successful_feature_generation():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        setup_isolated_repo(repo_path)

        supervisor = Supervisor("test-e2e", root=str(repo_path))

        csv_feature_code = (
            "import csv\nimport io\n\n"
            "def export_data(data):\n    return list(data)\n\n"
            "def export_csv(records, fieldnames):\n"
            "    output = io.StringIO()\n"
            "    writer = csv.DictWriter(output, fieldnames=fieldnames)\n"
            "    writer.writeheader()\n"
            "    writer.writerows(records)\n"
            "    return output.getvalue()\n"
        )

        test_csv_code = (
            "from exporter import export_data, export_csv\n\n"
            "def test_export_data():\n"
            "    assert export_data([1, 2]) == [1, 2]\n\n"
            "def test_export_csv():\n"
            "    res = export_csv([{'name': 'Alice'}], fieldnames=['name'])\n"
            "    assert 'Alice' in res\n"
        )

        changes = {
            "exporter.py": csv_feature_code,
            "test_exporter.py": test_csv_code,
        }

        result = supervisor.run_task(
            requirement="Add a function that exports records to CSV.",
            changes=changes,
            test_command=["pytest", "-q"],
        )

        assert result.success
        assert result.final_state == "COMPLETED"
        assert (repo_path / "exporter.py").read_text() == csv_feature_code
        assert "export_csv" in (repo_path / "exporter.py").read_text()

        # Check git commit was created for checkpoint acceptance
        git = GitTool(str(repo_path))
        status = git.status()
        assert status == ""  # Working tree clean after commit


def test_e2e_failed_candidate_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        setup_isolated_repo(repo_path)

        original_exporter = (repo_path / "exporter.py").read_text()

        supervisor = Supervisor("test-e2e-fail", root=str(repo_path))

        # Broken change with security issue and test failure
        broken_changes = {
            "exporter.py": "def export_data(data):\n    x = eval('data')\n    raise RuntimeError('Broken')\n",
        }

        result = supervisor.run_task(
            requirement="Intentionally broken candidate",
            changes=broken_changes,
            test_command=["pytest", "-q"],
        )

        assert not result.success
        assert result.final_state == "FAILED"

        # Verify filesystem changes were completely rolled back!
        assert (repo_path / "exporter.py").read_text() == original_exporter
        assert "eval" not in (repo_path / "exporter.py").read_text()
