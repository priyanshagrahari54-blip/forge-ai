from pathlib import Path

import pytest

from forge.models.model_studio import (
    BenchmarkCase,
    BenchmarkResult,
    ModelArtifact,
    ModelProvenance,
    ModelStudio,
    PromotionGate,
    SpecializationProfile,
    TrainingBackend,
    build_sft_record,
    stable_split,
)


def test_validate_dataset_removes_duplicates_and_secret_samples(tmp_path: Path):
    studio = ModelStudio(tmp_path)
    records = [
        {"instruction": "fix test", "output": "run pytest"},
        {"instruction": "fix test", "output": "run pytest"},
        {"instruction": "leak", "output": "api_key=sk-abcdefghijklmnopqrstuvwxyz"},
        {"instruction": "empty", "output": ""},
    ]
    clean, report = studio.validate_dataset(records)

    assert len(clean) == 1
    assert report.accepted == 1
    assert report.duplicates_removed == 1
    assert report.secret_bearing == 1
    assert not report.ok
    assert {item.code for item in report.issues} == {"DUPLICATE", "SECRET", "EMPTY"}


def test_split_is_deterministic_across_calls():
    records = [{"instruction": str(i), "output": "ok"} for i in range(100)]
    first = stable_split(records, validation_ratio=0.2, seed="same")
    second = stable_split(records, validation_ratio=0.2, seed="same")
    assert first == second
    assert len(first[0]) + len(first[1]) == len(records)


def test_training_plan_defaults_to_qlora_for_large_context(tmp_path: Path):
    studio = ModelStudio(tmp_path)
    profile = SpecializationProfile(
        name="forge-coding",
        description="software engineering specialist",
        domains=("python", "typescript", "systems"),
    )
    plan = studio.make_training_plan("base-model", profile, dataset_size=5000)
    assert plan.method == "qlora"
    assert plan.quantization == "4bit"
    assert plan.rank == 16
    assert plan.expected_train_examples == 5000


def test_promotion_rejects_regression():
    studio = ModelStudio(".")
    profile = SpecializationProfile("code", "coding", ("software",))
    provenance = ModelProvenance(
        artifact_id="candidate-1",
        base_model="base",
        specialization=profile.name,
        method="qlora",
        quantization="4bit",
        dataset_fingerprint="abc",
        trainer="test",
        trained=True,
    )
    artifact = ModelArtifact("candidate-1", "candidate", provenance)
    cases = [
        BenchmarkResult("coding", "coding", 0.95, True),
        BenchmarkResult("debug", "debugging", 0.60, False),
    ]
    promoted = studio.promote(
        artifact,
        cases,
        gate=PromotionGate(minimum_average=0.70, minimum_category=0.70, maximum_regression=0.05),
        baseline_by_category={"coding": 0.90, "debugging": 0.90},
    )
    assert promoted.status == "rejected"


def test_sft_record_refuses_secrets():
    with pytest.raises(ValueError):
        build_sft_record("do something", "token=ghp_abcdefghijklmnopqrstuvwxyz123456")


def test_training_requires_explicit_available_backend(tmp_path: Path):
    class FakeTrainer(TrainingBackend):
        name = "fake"

        def available(self):
            return True

        def train(self, plan, dataset, output_dir):
            provenance = ModelProvenance(
                artifact_id="trained-1",
                base_model=plan.base_model,
                specialization=plan.profile.name,
                method=plan.method,
                quantization=plan.quantization,
                dataset_fingerprint="abc",
                trainer=self.name,
                trained=True,
            )
            return ModelArtifact("trained-1", str(output_dir / "adapter"), provenance)

    studio = ModelStudio(tmp_path)
    profile = SpecializationProfile("code", "coding", ("software",))
    plan = studio.make_training_plan("base", profile, dataset_size=2)
    studio.register_trainer(FakeTrainer())
    artifact = studio.train(plan, [{"instruction": "x", "output": "y"}], trainer="fake", output_name="run")
    assert artifact.provenance.trained is True


def test_non_training_artifact_is_rejected(tmp_path: Path):
    class LyingTrainer(TrainingBackend):
        name = "lying"

        def available(self):
            return True

        def train(self, plan, dataset, output_dir):
            provenance = ModelProvenance(
                artifact_id="fake",
                base_model=plan.base_model,
                specialization=plan.profile.name,
                method=plan.method,
                quantization=plan.quantization,
                dataset_fingerprint="abc",
                trainer=self.name,
                trained=False,
            )
            return ModelArtifact("fake", str(output_dir / "adapter"), provenance)

    studio = ModelStudio(tmp_path)
    profile = SpecializationProfile("code", "coding", ("software",))
    plan = studio.make_training_plan("base", profile)
    studio.register_trainer(LyingTrainer())
    with pytest.raises(RuntimeError):
        studio.train(plan, [{"instruction": "x", "output": "y"}], trainer="lying", output_name="run")
