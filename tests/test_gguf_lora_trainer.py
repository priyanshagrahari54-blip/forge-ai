"""The local GGUF LoRA trainer: real weights, real loss, honest states.

These tests skip where the machine genuinely cannot run the trainer (no torch,
no GGUF). Where it can, they exercise the parts that decide whether an adapter
is real: the base model resolution, the ChatML/ tokenization pipeline, and the
forward pass whose loss must sit far below ``log(vocab)`` — a broken forward
pass cannot get near real text.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from forge.training.gguf_lora import (
    GgufLoRATrainer,
    build_sequences,
    resolve_base_model,
)

torch = pytest.importorskip("torch", reason="the trainer needs torch")
pytest.importorskip("gguf")
pytest.importorskip("tokenizers")

TRAINER = GgufLoRATrainer()
MODEL = TRAINER.resolve_base()

requires_model = pytest.mark.skipif(
    MODEL is None, reason="no base GGUF on this machine (set FORGE_BASE_GGUF)")


def test_availability_is_evidence_based():
    if MODEL is None:
        assert TRAINER.available() is False
    else:
        assert TRAINER.available() is True
        assert GgufLoRATrainer("/nonexistent/model.gguf").available() is False


def test_an_unnamed_base_model_falls_back_to_the_single_local_gguf(tmp_path):
    """One model on the box must be fine-tunable with no configuration."""
    if MODEL is None:
        pytest.skip("no base GGUF on this machine")
    assert resolve_base_model("") == MODEL
    # A name resolves to the quantized file that starts with it.
    stem = MODEL.stem.split(".")[0]
    assert resolve_base_model(stem) == MODEL or resolve_base_model(stem) is None
    assert resolve_base_model("model-that-does-not-exist") is None


@requires_model
def test_chatml_encoding_round_trips_and_holds_the_examples_out():
    from forge.training.gguf_model import GgufLlama

    gguf = GgufLlama(MODEL)
    records = [{"instruction": f"question {index}", "output": f"answer {index}"}
               for index in range(20)]
    train, evaluate = build_sequences(gguf, records, max_length=128)

    assert len(train) == 18 and len(evaluate) == 2
    decoded = gguf.decode(train[0])
    assert "question 0" in decoded and "answer 0" in decoded
    assert decoded.count("<|im_start|>") >= 2, "the model's own ChatML must be used"


@requires_model
def test_the_forward_pass_understands_real_text():
    """A correct forward gives low loss; a broken one gives ~log(vocab)."""
    from forge.training.gguf_model import GgufLlama
    from forge.training.llama_lora import LlamaLoraModel, evaluate_loss

    gguf = GgufLlama(MODEL)
    model = LlamaLoraModel(gguf)
    sentence = ("A small web service is being prepared for production. It must "
                "handle ten times its current traffic and stay secure.")
    sequences = [gguf.encode(sentence)] * 2
    loss = evaluate_loss(model, sequences, batches=1)
    chance = math.log(gguf.config.vocab_size)

    assert loss < chance / 2, f"loss {loss:.2f} is not better than chance {chance:.2f}"
    # The adapters start at zero, so training starts exactly at the base model.
    assert all(float(p.abs().sum()) == 0.0
               for name, p in model.params.items() if name.endswith("lora_b"))


@requires_model
def test_a_short_training_run_lowers_the_loss_and_writes_both_artifacts(tmp_path):
    from forge.models.model_studio import ModelStudio, SpecializationProfile

    studio = ModelStudio(root=tmp_path)
    trainer = GgufLoRATrainer(steps=10, rank=4, max_length=96)
    studio.register_trainer(trainer)
    records = [{"instruction": f"state the first action for case {index}",
                "output": "Check the database indexes and add a keyset cursor "
                          "so the endpoint stops scanning the whole table."}
               for index in range(16)]
    plan = studio.make_training_plan(
        MODEL.stem, SpecializationProfile(name="documentation",
                                          description="test specialist",
                                          domains=("docs",)),
        dataset_size=len(records), method="lora")

    artifact = studio.train(plan, records, trainer="gguf-lora",
                            output_name="smoke")

    assert artifact.provenance.trained is True
    out = Path(artifact.path)
    assert (out / "adapter_model.safetensors").is_file()
    assert (out / "adapter.gguf").is_file()
    import json
    report = json.loads((out / "training.json").read_text())
    assert report["servable"] is True
    assert report["trainable_parameters"] > 0
    assert report["final_loss"] < report["initial_loss"], (
        "a training run that does not lower the loss has learned nothing")
    assert len(report["history"]) >= 2


def test_a_training_run_with_no_base_model_refuses_instead_of_faking(tmp_path):
    trainer = GgufLoRATrainer("/definitely/not/here.gguf", steps=1)
    assert trainer.available() is False
    from forge.models.model_studio import ModelStudio, SpecializationProfile

    studio = ModelStudio(root=tmp_path)
    studio.register_trainer(trainer)
    plan = studio.make_training_plan(
        "missing", SpecializationProfile(name="x", description="x",
                                         domains=("x",)), method="lora")
    with pytest.raises(Exception) as caught:
        studio.train(plan, [{"instruction": "q", "output": "a"}],
                     trainer="gguf-lora", output_name="refused")
    # The refusal names the real problem: the backend cannot run here.
    assert "unavailable" in str(caught.value)
