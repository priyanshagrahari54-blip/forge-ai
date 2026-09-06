"""Real model-contract E2E: the test supplies a provider, not pre-written changes to Forge."""
import json, subprocess, sys
from pathlib import Path
from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.provider import MockProvider
from forge.models.router import ModelInfo, ModelRouter
from forge.security.verification import VerificationPipeline
from forge.tools.checkpoint import CheckpointManager
from forge.tools.git import GitTool

def test_autonomous_csv_feature_and_gates(tmp_path):
    (tmp_path / "app.py").write_text("def rows(): return [{'name':'Ada','score':3}]\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("from app import rows\ndef test_rows(): assert rows()[0]['name']=='Ada'\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    git=GitTool(tmp_path); git.run("add", "--", "app.py", "tests/test_app.py"); git.run("commit", "-m", "initial")
    changes={"app.py": "import csv\nfrom io import StringIO\n\ndef rows(): return [{'name':'Ada','score':3}]\n\ndef export_csv():\n    output=StringIO()\n    writer=csv.DictWriter(output, fieldnames=['name','score'], lineterminator='\\n')\n    writer.writeheader(); writer.writerows(rows())\n    return output.getvalue()\n", "tests/test_csv.py": "from app import export_csv\ndef test_csv_export():\n    assert export_csv() == 'name,score\\nAda,3\\n'\n"}
    provider=MockProvider(json.dumps({"changes": changes, "explanation": "Added CSV export and regression coverage"}))
    router=ModelRouter([ModelInfo("test-local-model", "coding", available=True, free=True, provider=provider, capabilities=("coding",))])
    task=TaskEngine().add("csv", "Add CSV export functionality and tests")
    context=CoderAgent(root=str(tmp_path), router=router).build_context(RepositoryIntelligence.build(tmp_path), task.description)
    response=CoderAgent(root=str(tmp_path), router=router).execute(AgentRequest(task, TaskStatus.CODING, context=context, metadata={"approved": True}))
    assert response.success and response.metadata["files"] == ["app.py", "tests/test_csv.py"]
    verification=VerificationPipeline(tmp_path).run(git.diff())
    assert verification.passed, [(g.name,g.details) for g in verification.failures]
    git.stage_files(response.metadata["files"])
    assert git.run("diff", "--cached", "--name-only").stdout.splitlines() == response.metadata["files"]
    assert git.run("commit", "-m", "feat: add csv export").returncode == 0

def test_checkpoint_restores_exact_state_without_git_reset(tmp_path):
    original=tmp_path/"file.txt"; original.write_text("before\n")
    unrelated=tmp_path/"unrelated.txt"; unrelated.write_text("keep\n")
    manager=CheckpointManager(tmp_path); checkpoint=manager.create("rollback")
    original.write_text("after\n"); (tmp_path/"new.txt").write_text("new\n")
    manager.rollback(checkpoint, ["file.txt", "new.txt"])
    assert original.read_text()=="before\n" and unrelated.read_text()=="keep\n" and not (tmp_path/"new.txt").exists()
