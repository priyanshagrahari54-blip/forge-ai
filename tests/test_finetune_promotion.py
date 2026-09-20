"""The fine-tune promotion gate: trained is not the same as promoted.

Two layers, so something real runs everywhere:

* the scoring and held-out split are pure functions and are tested on any
  machine, including the torch-free CI image;
* the end-to-end comparison (train a tiny adapter, generate with it, let the
  gate decide) runs only where torch, gguf, tokenizers, safetensors and the
  base GGUF exist — and it asserts that the gate's verdict is what the artifact
  carries, including the honest ``rejected`` outcome.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.models.model_studio import ModelStudio, PromotionGate, stable_split
from forge.training.promotion import (
    answer_similarity,
    evaluate_artifact,
    held_out_records,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Pure scoring and split
# ---------------------------------------------------------------------------

def test_identical_answers_score_one():
    text = "Add a cache layer in front of the database and measure it"
    assert answer_similarity(text, text) == pytest.approx(1.0)


def test_disjoint_answers_score_zero():
    assert answer_similarity("completely unrelated words", "cache index shard") == 0.0


def test_missing_text_scores_zero():
    assert answer_similarity("", "cache") == 0.0
    assert answer_similarity("cache", "") == 0.0


def test_partial_overlap_sits_between_the_extremes():
    score = answer_similarity("add cache layer now",
                              "add cache layer to the database and measure")
    assert 0.0 < score < 1.0


def test_similarity_ignores_case_and_punctuation():
    assert answer_similarity("Add Cache, layer!",
                             "add cache layer") == pytest.approx(1.0)


def test_held_out_split_is_deterministic_and_disjoint():
    records = [{"instruction": f"q{index}", "output": f"a{index}"}
               for index in range(20)]
    first = held_out_records(records, ratio=0.25)
    second = held_out_records(records, ratio=0.25)
    assert [r["instruction"] for r in first] == [r["instruction"] for r in second]
    assert len(first) == 5
    train, _ = stable_split(records, validation_ratio=0.25)
    assert not ({r["instruction"] for r in first}
                & {r["instruction"] for r in train})


def test_held_out_needs_two_usable_rows():
    assert held_out_records([{"instruction": "q", "output": "a"}]) == []
    assert held_out_records([{"instruction": "q", "output": ""}]) == []


def test_skip_notes_are_recorded_when_the_base_model_is_absent(tmp_path):
    """A promotion run without the deployed GGUF reports why, it does not guess."""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "training.json").write_text(json.dumps({
        "base_model": str(tmp_path / "missing.gguf"),
        "specialization": "coding",
        "rank": 4,
        "alpha": 8,
        "targets": ["attn_q", "attn_v"],
    }), encoding="utf-8")

    from forge.models.model_studio import (
        ModelArtifact,
        ModelProvenance,
    )

    artifact = ModelArtifact(
        id="missing-base", path=str(artifact_dir), status="candidate",
        provenance=ModelProvenance(
            artifact_id="missing-base", base_model="missing.gguf",
            specialization="coding", method="lora", quantization="none",
            dataset_fingerprint="", trainer="gguf-lora", trained=True))

    outcome = evaluate_artifact(
        ModelStudio(root=str(tmp_path / "studio")), artifact,
        [{"instruction": "q1", "output": "a1"},
         {"instruction": "q2", "output": "a2"}])

    assert outcome.cases == 0
    assert outcome.promoted is False
    assert artifact.status == "candidate"
    assert any("base GGUF not found" in note for note in outcome.notes)


# ---------------------------------------------------------------------------
# End to end: train a tiny adapter, compare it with the base model, gate it
# ---------------------------------------------------------------------------

torch = pytest.importorskip("torch", reason="promotion generation needs torch")
pytest.importorskip("gguf")
pytest.importorskip("tokenizers")
pytest.importorskip("safetensors")

from forge.models.builtin_profiles import FORGE_CODING      # noqa: E402
from forge.training.gguf_lora import GgufLoRATrainer        # noqa: E402
from forge.training.job import register_default_trainers    # noqa: E402

TRAINER = GgufLoRATrainer()
MODEL = TRAINER.resolve_base()

requires_model = pytest.mark.skipif(
    MODEL is None, reason="no base GGUF on this machine (set FORGE_BASE_GGUF)")


def _records() -> list[dict[str, str]]:
    brief = ("As the {role} specialist, state the first concrete action "
             "you would take and why.")
    return [{"instruction": brief.format(role="coding"),
             "output": "Profile the query path, then add a cache in front of "
                       "the database and measure the hit rate."},
            {"instruction": brief.format(role="debugging"),
             "output": "Reproduce the failing case, then bisect the change "
                       "that introduced it."},
            {"instruction": brief.format(role="review"),
             "output": "Read the diff for correctness first, then for "
                       "missing tests."}]


@requires_model
def test_training_then_promotion_decision_is_recorded(tmp_path):
    studio = ModelStudio(root=str(tmp_path / "studio"))
    assert "gguf-lora" in register_default_trainers(
        studio, base_model=str(MODEL), steps=2)

    records = _records()
    plan = studio.make_training_plan(str(MODEL), FORGE_CODING,
                                     dataset_size=len(records))
    artifact = studio.train(plan, records, trainer="gguf-lora",
                            output_name="promotion-test")
    assert artifact.status == "candidate"

    #: A perfectly reachable gate: this asserts the *mechanism* (real
    #: generation, real scoring, a recorded verdict), not that two optimizer
    #: steps produce a good adapter.
    gate = PromotionGate(minimum_average=0.0, minimum_category=0.0,
                         maximum_regression=1.0, require_all_critical=False)
    outcome = evaluate_artifact(studio, artifact, records,
                                specialization="coding", gate=gate,
                                max_cases=2, max_new_tokens=4)

    assert outcome.cases == 2, outcome.notes
    assert len(outcome.baseline) == 2
    #: ``promote`` is immutable-style: the verdict lives on the returned
    #: artifact, and the trained candidate is left untouched.
    assert outcome.artifact.status == "promoted"
    assert outcome.promoted is True
    assert artifact.status == "candidate"
    assert Path(outcome.manifest).is_file()
    manifest = json.loads(Path(outcome.manifest).read_text("utf-8"))
    assert manifest["artifact"]["status"] == "promoted"
    assert len(manifest["benchmarks"]) == 2
    #: Every score is computed from generated text, never defaulted.
    assert all(0.0 <= result["score"] <= 1.0 for result in manifest["benchmarks"])


@requires_model
def test_an_impossible_gate_rejects_instead_of_promoting(tmp_path):
    studio = ModelStudio(root=str(tmp_path / "studio"))
    assert "gguf-lora" in register_default_trainers(
        studio, base_model=str(MODEL), steps=2)

    records = _records()
    plan = studio.make_training_plan(str(MODEL), FORGE_CODING,
                                     dataset_size=len(records))
    artifact = studio.train(plan, records, trainer="gguf-lora",
                            output_name="promotion-reject")
    gate = PromotionGate(minimum_average=1.0, minimum_category=1.0,
                         maximum_regression=0.0, require_all_critical=True)
    outcome = evaluate_artifact(studio, artifact, records,
                                specialization="coding", gate=gate,
                                max_cases=1, max_new_tokens=4)

    assert outcome.promoted is False
    assert outcome.artifact.status == "rejected"
    assert artifact.status == "candidate"
    assert any("gate rejected" in note for note in outcome.notes)
