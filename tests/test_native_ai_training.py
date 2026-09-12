"""Future-training interfaces: real mechanics, zero pretending.

The contract: datasets only ever contain verified real model output; the
validator rejects secrets; *no* training can run; versions can only be
registered over artifacts that exist; promotion needs a passing, recorded
evaluation; and the status surface always reports the truth (zero trained
models until files exist on disk).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers_native_ai import write_repo

from forge.native.engine import NativeAIEngine
from forge.native.training import (
    DatasetBuilder,
    DatasetValidator,
    ModelVersionStore,
    NativeTrainingFabric,
    TrainingJobStore,
    TrainingRuntimeNotConfigured,
    BenchmarkComparator,
)


def test_dataset_empty_without_real_verified_runs(tmp_path):
    write_repo(tmp_path)
    report = DatasetBuilder(tmp_path).build()
    assert report["examples"] == 0 and report["empty"]
    assert "fabricate" in report["reason"]
    assert not (tmp_path / ".forge/native/training/datasets").exists() or \
        list((tmp_path / ".forge/native/training/datasets").glob("*.jsonl")) \
        == []


def test_dataset_excludes_deterministic_and_uncaptured_runs(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    engine.run("analyze the repository")
    report = DatasetBuilder(tmp_path).collect()
    examples, notes = report
    assert examples == []  # no model output exists in this installation


def test_dataset_round_trip_from_a_real_verified_captured_run(tmp_path):
    from helpers_native_ai import (CALC_BROKEN, CALC_GOOD, ScriptedProvider,
                                   changes_text, scripted_fabric)
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False, dataset_capture=True)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "COMPLETED"
    record_path = tmp_path / ".forge/native/runs" / (
        result.report.run_id + ".json")
    assert record_path.is_file()
    examples, notes = DatasetBuilder(tmp_path).collect()
    assert len(examples) == 1
    example = examples[0]
    assert example.model == "scripted-model" and example.verified
    # completion is the *actual* raw model text (kept verbatim, redaction
    # applied at report level): parsing it must yield the applied change.
    payload = json.loads(example.completion)
    assert payload["changes"]["calc.py"] == CALC_GOOD
    assert json.loads(example.prompt)["requirement"].startswith("fix")
    built = DatasetBuilder(tmp_path).build()
    assert built["examples"] == 1 and not built.get("empty")
    dataset_path = Path(built["dataset"])
    validation = DatasetValidator().validate(dataset_path)
    assert validation["ok"], validation
    assert validation["examples"] == 1


def test_dataset_skips_runs_when_capture_was_off(tmp_path):
    from helpers_native_ai import (CALC_BROKEN, CALC_GOOD, ScriptedProvider,
                                   changes_text, scripted_fabric)
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False, dataset_capture=False)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "COMPLETED"
    examples, notes = DatasetBuilder(tmp_path).collect()
    assert examples == []  # verified, yet not eligible: capture was off
    assert any("dataset_capture" in note for note in notes)


def test_dataset_excludes_failed_verification_runs(tmp_path):
    from helpers_native_ai import (CALC_BROKEN, ScriptedProvider,
                                   changes_text, scripted_fabric)
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": "def add(a, b):\n    return a * b\n"}))  # still broken
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False, dataset_capture=True,
                            max_debug_retries=0)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "FAILED"
    examples, _notes = DatasetBuilder(tmp_path).collect()
    assert examples == []  # never learn from runs that did not pass


def test_validator_rejects_bad_schema_duplicates_and_secrets(tmp_path):
    good = json.dumps({"prompt": "p", "completion": "c",
                       "model": "m"})
    dup = good
    bad_shape = json.dumps({"prompt": "only prompt"})
    secret = json.dumps({"prompt": "p", "completion": "sk-" + "x" * 24,
                         "model": "m"})
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join([good, dup, bad_shape, secret]) + "\n",
                    encoding="utf-8")
    report = DatasetValidator().validate(path)
    assert not report["ok"]
    joined = " ".join(report["errors"])
    assert "missing" in joined           # bad shape flagged
    assert "secret" in joined            # credentials flagged
    assert report["duplicates"] == 1     # exact duplicate flagged
    assert report["examples"] >= 1


def test_job_creation_requires_validated_dataset(tmp_path):
    write_repo(tmp_path)
    missing = tmp_path / "ghost.jsonl"
    with pytest.raises(FileNotFoundError):
        TrainingJobStore(tmp_path).create(missing)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n", encoding="utf-8")
    job = TrainingJobStore(tmp_path).create(bad)
    assert job["status"] == "FAILED_PRECHECK"
    assert job["ran"] is False
    assert "invalid JSON" in job["error"]


def test_training_run_is_honestly_refused(tmp_path):
    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps({"prompt": "p", "completion": "c",
                                "model": "m"}) + "\n", encoding="utf-8")
    store = TrainingJobStore(tmp_path)
    job = store.create(good)
    with pytest.raises(TrainingRuntimeNotConfigured) as excinfo:
        store.run(job["id"])
    message = str(excinfo.value)
    assert "no training runtime is configured" in message
    # the job stays recorded and un-executed:
    listed = store.list()
    assert listed[0]["status"] == "RECORDED" and listed[0]["ran"] is False


def test_version_registration_requires_existing_artifact(tmp_path):
    store = ModelVersionStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.register("v1", "models/does-not-exist.bin")
    artifact = tmp_path / "models" / "v1.bin"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_bytes(b"real bytes")
    manifest = store.register("v1", artifact, dataset_sha256="abc")
    assert manifest["version"] == "v1"
    assert store.list()
    # promotion needs a passing evaluation — never just a registration:
    with pytest.raises(ValueError) as excinfo:
        store.promote("v1")
    assert "evaluation" in str(excinfo.value)


def test_promotion_and_rollback_are_pointer_operations(tmp_path):
    store = ModelVersionStore(tmp_path)
    for name in ("v1", "v2"):
        artifact = tmp_path / "models" / (name + ".bin")
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(b"bytes-" + name.encode())
        store.register(name, artifact)
        ModelVersionStore(tmp_path)  # store re-reads from disk
    versions = tmp_path / ".forge/native/training/models/versions"
    for path in versions.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["evaluation"] = {"passed": True, "tasks": 5, "passed_ratio": 1.0}
        path.write_text(json.dumps(data), encoding="utf-8")
    assert store.promote("v1")["active"] == "v1"
    assert store.active()["active"] == "v1"
    assert store.promote("v2")["previous"] == "v1"
    assert store.rollback()["active"] == "v1"
    assert store.active()["manifest"]["version"] == "v1"
    # rolling back again (no earlier version) clears the pointer honestly:
    store.promote("v2")
    (tmp_path / ".forge/native/training/models/active.json").write_text(
        json.dumps({"active": "v2", "previous": "", "history": []}),
        encoding="utf-8")
    assert store.rollback()["active"] is None


def test_benchmark_comparator_only_compares_measurements():
    report = BenchmarkComparator.compare(
        {"passed_ratio": 0.8, "tasks": 10},
        {"passed_ratio": 0.6, "tasks": 10})
    assert report["winner"] == "a"
    tie = BenchmarkComparator.compare({"passed_ratio": 0.5, "tasks": 2},
                                      {"passed_ratio": 0.5, "tasks": 2})
    assert tie["winner"] == "tie"


def test_training_status_reports_zero_until_models_exist(tmp_path):
    write_repo(tmp_path)
    status = NativeTrainingFabric(tmp_path).status()
    assert status["trained_models"] == 0
    assert status["trainer"] == "not_configured"
    assert "no training run has produced a Forge model yet" in status["note"]
    assert status["active_model"] is None


def test_evaluation_without_model_refuses(tmp_path):
    from forge.native.training import ModelEvaluator
    with pytest.raises(TrainingRuntimeNotConfigured):
        ModelEvaluator(tmp_path, fabric=None).evaluate(["fix x"])
    from forge.models import ModelFabric, ModelRegistry, ProviderRegistry
    empty = ModelFabric(registry=ModelRegistry(),
                        providers=ProviderRegistry())
    with pytest.raises(TrainingRuntimeNotConfigured):
        ModelEvaluator(tmp_path, fabric=empty).evaluate(["fix x"])
