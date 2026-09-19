"""The fine-tuning pipeline stays honest: real data, real states, no pretending.

Two things are pinned here.

1. The dataset comes from evidence. A fleet run's records become instruction
   pairs, blocked specialists contribute nothing (there is no answer to learn
   from), duplicates collapse, and a secret-bearing record is rejected by the
   studio's validator rather than trained on.

2. A job never claims more than it did. With no training stack and no trainer
   the state is BLOCKED and the blockers name the exact missing requirement; a
   backend that raises produces FAILED with the verbatim error; only a real
   artifact the backend itself marked ``trained`` yields TRAINED.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.models.model_studio import ModelArtifact, ModelProvenance, ModelStudio
from forge.training.dataset import build_dataset, records_from_evidence
from forge.training.job import FineTuneJob, JobState, register_default_trainers


def _evidence(path: Path, records) -> Path:
    path.write_text(json.dumps({"summary": {}, "records": records}),
                    encoding="utf-8")
    return path


def test_instruction_pairs_come_from_real_outputs_only(tmp_path):
    artifact = _evidence(tmp_path / "run.json", [
        {"agent": "coder-01-0004", "role": "coding", "success": True,
         "required_capability": "coding",
         "output": "Add pagination with a keyset cursor."},
        {"agent": "vision-01-0019", "role": "vision", "success": False,
         "required_capability": "vision", "output": "",
         "error": "no registered model supports capabilities ['vision']"},
        {"agent": "tester-01-0010", "role": "testing", "success": True,
         "required_capability": "testing",
         "output": "Write a load test that asserts p95 latency."},
    ])
    records = records_from_evidence(json.loads(artifact.read_text()))

    assert [r["role"] for r in records] == ["coding", "testing"]
    assert all(r["response"] for r in records)
    assert "coding specialist" in records[0]["prompt"], (
        "the prompt must state the role the answer belongs to")


def test_a_dataset_is_validated_deduplicated_and_fingerprinted(tmp_path):
    artifact = _evidence(tmp_path / "run.json", [
        {"agent": "a", "role": "coding", "success": True,
         "required_capability": "coding", "output": "Same answer."},
        {"agent": "b", "role": "coding", "success": True,
         "required_capability": "coding", "output": "Same answer."},
        {"agent": "c", "role": "testing", "success": True,
         "required_capability": "testing", "output": "Different answer."},
    ])
    studio = ModelStudio(root=tmp_path)
    bundle = build_dataset([artifact], studio=studio,
                           output=tmp_path / "dataset.jsonl")

    assert len(bundle.records) == 2, "duplicates must collapse"
    assert bundle.report["duplicates_removed"] == 1
    assert bundle.by_role == {"coding": 1, "testing": 1}
    assert bundle.fingerprint
    lines = bundle.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["instruction"]


def test_a_secret_bearing_answer_is_rejected_not_trained_on(tmp_path):
    artifact = _evidence(tmp_path / "run.json", [
        {"agent": "a", "role": "coding", "success": True,
         "required_capability": "coding",
         "output": "Set OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345"},
        {"agent": "b", "role": "coding", "success": True,
         "required_capability": "coding", "output": "Use a keyset cursor."},
    ])
    studio = ModelStudio(root=tmp_path)
    bundle = build_dataset([artifact], studio=studio,
                           output=tmp_path / "dataset.jsonl")

    assert len(bundle.records) == 1
    assert bundle.report["secret_bearing"] == 1
    assert "sk-proj" not in bundle.path.read_text(encoding="utf-8")


def test_a_missing_training_stack_blocks_with_the_exact_requirement(tmp_path,
                                                                    monkeypatch):
    artifact = _evidence(tmp_path / "run.json", [
        {"agent": f"a{i}", "role": "coding", "success": True,
         "required_capability": "coding", "output": f"Answer {i}."}
        for i in range(10)
    ])
    studio = ModelStudio(root=tmp_path)
    bundle = build_dataset([artifact], studio=studio,
                           output=tmp_path / "dataset.jsonl")

    job = FineTuneJob(specialization="coding", dataset_path=bundle.path,
                      base_model="SmolLM2-135M-Instruct",
                      records=bundle.records, studio=studio,
                      trainer="huggingface-peft")
    state = job.preflight(minimum_records=8)

    # Force the condition regardless of what this machine happens to have
    # installed: the blocked path must be exercised, not skipped.
    monkeypatch.setattr("forge.training.job._importable", lambda name: None)
    job.trainer = "huggingface-peft"
    state = job.preflight(minimum_records=8)

    assert state is JobState.BLOCKED
    assert any("training stack" in blocker for blocker in job.blockers)
    assert any(requirement.startswith("pip install")
               for requirement in job.requirements)
    # A registered trainer is reported as registered, and the state is still
    # BLOCKED because the stack is missing: one requirement is not the other.
    studio.register_trainer(_NullBackend())
    monkeypatch.undo()
    assert job.preflight(minimum_records=8) is JobState.BLOCKED
    # Never TRAINED, and running must not change that.
    assert job.run(minimum_records=8) is None
    assert job.state is JobState.BLOCKED
    assert job.artifact is None


class _NullBackend:
    name = "null"

    def available(self) -> bool:
        return True

    def train(self, plan, dataset, output_dir):        # pragma: no cover
        raise AssertionError("this backend must never be asked to train")


def test_a_tiny_dataset_is_refused_rather_than_trained_on(tmp_path):
    studio = ModelStudio(root=tmp_path)
    job = FineTuneJob(specialization="coding",
                      dataset_path=tmp_path / "d.jsonl",
                      base_model="m", records=[{"instruction": "i", "output": "o"}],
                      studio=studio, trainer="none")
    assert job.preflight(minimum_records=8) is JobState.BLOCKED
    assert any("1 usable examples" in blocker for blocker in job.blockers)


class _RecordingBackend:
    """A backend that behaves like a real trainer: it returns an artifact."""

    name = "recording"

    def __init__(self, *, trained: bool = True, explode: bool = False):
        self.trained = trained
        self.explode = explode
        self.seen = []

    def available(self) -> bool:
        return True

    def train(self, plan, dataset, output_dir):
        self.seen.append({"plan": plan, "dataset": len(list(dataset)),
                          "output_dir": str(output_dir)})
        if self.explode:
            raise RuntimeError("CUDA out of memory")
        return ModelArtifact(
            id="artifact-1", path=str(output_dir),
            provenance=ModelProvenance(
                artifact_id="artifact-1", base_model=plan.base_model,
                specialization=plan.profile.name, method=plan.method,
                quantization=plan.quantization, dataset_fingerprint="fp",
                trainer=self.name, trained=self.trained),
            status="candidate")


def test_a_real_backend_run_reaches_trained_with_provenance(tmp_path):
    studio = ModelStudio(root=tmp_path)
    backend = _RecordingBackend()
    studio.register_trainer(backend)
    records = [{"instruction": f"q{i}", "output": f"a{i}",
                "capability": "coding"} for i in range(12)]

    job = FineTuneJob(specialization="coding", dataset_path=tmp_path / "d.jsonl",
                      base_model="SmolLM2-135M-Instruct", records=records,
                      studio=studio, trainer="recording", epochs=1)
    assert job.preflight(minimum_records=8) is JobState.READY
    artifact = job.run(minimum_records=8)

    assert job.state is JobState.TRAINED
    assert artifact is not None and artifact.provenance.trained is True
    assert job.to_dict()["artifact"]["provenance"]["trainer"] == "recording"
    assert backend.seen[0]["dataset"] == 12
    # The plan the studio built carries the specialization, not a default.
    assert backend.seen[0]["plan"].profile.name == "coding"


def test_a_backend_failure_is_reported_verbatim_and_never_trained(tmp_path):
    studio = ModelStudio(root=tmp_path)
    studio.register_trainer(_RecordingBackend(explode=True))
    records = [{"instruction": f"q{i}", "output": f"a{i}"} for i in range(12)]

    job = FineTuneJob(specialization="coding", dataset_path=tmp_path / "d.jsonl",
                      base_model="m", records=records, studio=studio,
                      trainer="recording")
    assert job.run(minimum_records=8) is None
    assert job.state is JobState.FAILED
    assert "CUDA out of memory" in job.error
    assert job.artifact is None


def test_an_artifact_the_backend_did_not_mark_trained_is_refused(tmp_path):
    """The studio enforces this; a wrong TRAINED is worse than a missing one."""
    studio = ModelStudio(root=tmp_path)
    studio.register_trainer(_RecordingBackend(trained=False))
    records = [{"instruction": f"q{i}", "output": f"a{i}"} for i in range(12)]

    job = FineTuneJob(specialization="coding", dataset_path=tmp_path / "d.jsonl",
                      base_model="m", records=records, studio=studio,
                      trainer="recording")
    assert job.run(minimum_records=8) is None
    assert job.state is JobState.FAILED
    assert job.artifact is None
