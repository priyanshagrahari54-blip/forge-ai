"""Decide promotion for a trained adapter from real held-out measurements.

Training is not evidence that an adapter is better. This module closes the loop
the Model Studio leaves open: it loads the artifact the trainer wrote (the same
GGUF the runtime serves, plus the adapter's own safetensors), generates an
answer with the base model and with the adapter on prompts the trainer never
saw, scores both against the recorded reference answers, and hands the results
to :meth:`forge.models.model_studio.ModelStudio.promote` so the promotion gate
decides.

Everything here is real: the weights are the deployed ones, the adapter's own
LoRA deltas drive the second generation, the scoring is computed from the
generated text, and a rejection is a legitimate outcome — the gate is allowed
to say no. Nothing is written as "promoted" unless the gate returned promoted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from forge.models.model_studio import (
    BenchmarkResult,
    ModelArtifact,
    ModelStudio,
    PromotionGate,
    stable_split,
)
from forge.training.gguf_model import GgufLlama
from forge.training.gguf_lora import (
    CHAT_SYSTEM,
    CHATML_ASSISTANT,
    CHATML_SYSTEM,
    CHATML_USER,
    resolve_base_model,
)
from forge.training.llama_lora import LlamaLoraModel, LoRAConfig

#: Word tokens, so the score does not reward punctuation or casing.
_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def answer_similarity(generated: str, reference: str) -> float:
    """Token-level F1 between a short answer and the recorded one.

    F1 rather than exact match: a specialist states the same first action in
    its own words, and neither a longer nor a shorter phrasing should win.
    """
    produced = _terms(generated)
    expected = _terms(reference)
    if not produced or not expected:
        return 0.0
    remaining = list(expected)
    hits = 0
    for token in produced:
        if token in remaining:
            remaining.remove(token)
            hits += 1
    if not hits:
        return 0.0
    precision = hits / float(len(produced))
    recall = hits / float(len(expected))
    return 2 * precision * recall / (precision + recall)


def held_out_records(records: Sequence[Mapping[str, Any]], *,
                     ratio: float = 0.25) -> list[Mapping[str, Any]]:
    """The validation slice of the dataset, deterministically chosen.

    Uses the studio's own hash split, so the same dataset always evaluates on
    the same rows and the training slice is the same one the trainer used.
    """
    usable = [record for record in records
              if str(record.get("instruction") or record.get("prompt") or "").strip()
              and str(record.get("output") or record.get("response") or "").strip()]
    if len(usable) < 2:
        return []
    _, validation = stable_split(usable, validation_ratio=ratio)
    return validation


def _prompt_of(record: Mapping[str, Any]) -> str:
    return str(record.get("instruction") or record.get("prompt") or "").strip()


def _reference_of(record: Mapping[str, Any]) -> str:
    return str(record.get("output") or record.get("response") or "").strip()


def _chatml(prompt: str) -> str:
    """The prompt a chat model continues: the assistant turn stays open.

    ``CHATML_ASSISTANT`` with an empty response closes the turn, which asks the
    model to continue *after* the end-of-turn marker and produced token soup.
    Generation needs the opening tag only.
    """
    return (CHATML_SYSTEM.format(system=CHAT_SYSTEM)
            + CHATML_USER.format(prompt=prompt)
            + "<|im_start|>assistant\n")


def greedy_answer(gguf: GgufLlama, model: LlamaLoraModel, prompt: str, *,
                  max_new_tokens: int = 32) -> str:
    """Generate a short answer with the loaded weights (greedy, deterministic).

    Written for evidence, not for conversation: no sampling, no stop strings
    beyond the model's own end-of-turn markers, so a rerun reproduces the same
    text and the score is a property of the adapter rather than of a seed.
    """
    torch = model.torch
    tokens = list(gguf.encode(_chatml(prompt)))
    produced: list[int] = []
    with torch.no_grad():
        for _ in range(max(1, int(max_new_tokens))):
            ids = torch.tensor([tokens], dtype=torch.long)
            logits = model.forward(ids)[0, -1]
            next_id = int(torch.argmax(logits))
            tokens.append(next_id)
            produced.append(next_id)
            text = gguf.decode(produced)
            if "<|im_end|>" in text or "<|endoftext|>" in text:
                break
    return gguf.decode(produced).strip()


@dataclass
class EvaluationOutcome:
    """What the held-out measurement found, and what the gate did with it."""

    artifact: ModelArtifact
    results: list[BenchmarkResult] = field(default_factory=list)
    baseline: list[BenchmarkResult] = field(default_factory=list)
    average: float = 0.0
    baseline_average: float = 0.0
    cases: int = 0
    notes: list[str] = field(default_factory=list)
    manifest: str = ""

    @property
    def promoted(self) -> bool:
        return self.artifact.status == "promoted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact.id,
            "status": self.artifact.status,
            "promoted": self.promoted,
            "cases": self.cases,
            "average": round(self.average, 4),
            "baseline_average": round(self.baseline_average, 4),
            "delta": round(self.average - self.baseline_average, 4),
            "manifest": self.manifest,
            "notes": list(self.notes),
            "cases_detail": [result.__dict__ for result in self.results],
            "baseline_detail": [result.__dict__ for result in self.baseline],
        }


def _load_adapter(model: LlamaLoraModel, artifact_dir: Path) -> None:
    """Copy the trained tensors into the model's LoRA parameters.

    The trainer writes ``adapter_model.safetensors`` with the same keys the
    forward pass reads, so promotion scores the artifact that was written —
    not a freshly initialised adapter.
    """
    from safetensors.torch import load_file

    path = artifact_dir / "adapter_model.safetensors"
    if not path.is_file():
        raise FileNotFoundError(f"adapter weights missing: {path}")
    trained = load_file(str(path))
    for name, tensor in trained.items():
        if name in model.params:
            model.params[name] = tensor.to(model.torch.float32)


def _adapter_config(artifact_dir: Path) -> tuple[dict[str, Any], LoRAConfig]:
    metadata = json.loads((artifact_dir / "training.json").read_text("utf-8"))
    config = LoRAConfig(
        targets=tuple(metadata.get("targets") or ("attn_q", "attn_v")),
        rank=int(metadata.get("rank") or 8),
        alpha=int(metadata.get("alpha") or 16),
    )
    return metadata, config


def evaluate_artifact(studio: ModelStudio, artifact: ModelArtifact,
                      records: Sequence[Mapping[str, Any]], *,
                      specialization: str = "",
                      gate: PromotionGate | None = None,
                      max_cases: int = 8, max_new_tokens: int = 32,
                      ratio: float = 0.25) -> EvaluationOutcome:
    """Score base vs adapter on held-out evidence and apply the promotion gate.

    Returns the outcome whatever the gate decides. The base model is evaluated
    with a zero-initialised adapter (which is exactly the base function), so the
    comparison isolates what training changed.
    """
    artifact_dir = Path(artifact.path)
    metadata, config = _adapter_config(artifact_dir)
    base_path = resolve_base_model(str(metadata.get("base_model") or ""))
    outcome = EvaluationOutcome(artifact=artifact)
    if base_path is None:
        outcome.notes.append(
            "base GGUF not found; promotion needs the model the runtime serves")
        return outcome

    validation = held_out_records(records, ratio=ratio)
    if not validation:
        outcome.notes.append(
            "dataset has no held-out rows to evaluate on (needs >= 2 examples)")
        return outcome
    cases = validation[:max(1, int(max_cases))]

    gguf = GgufLlama(base_path)
    candidate = LlamaLoraModel(gguf, config)
    _load_adapter(candidate, artifact_dir)
    baseline = LlamaLoraModel(gguf, config)  #: zero-init LoRA == base model

    category = specialization or metadata.get("specialization") or "general"
    for index, record in enumerate(cases):
        prompt, reference = _prompt_of(record), _reference_of(record)
        case_id = f"{category}-{index + 1}"
        produced = greedy_answer(gguf, candidate, prompt,
                                 max_new_tokens=max_new_tokens)
        base_text = greedy_answer(gguf, baseline, prompt,
                                  max_new_tokens=max_new_tokens)
        score = answer_similarity(produced, reference)
        base_score = answer_similarity(base_text, reference)
        outcome.results.append(BenchmarkResult(
            case_id=case_id, category=category, score=score,
            passed=score >= 0.5, evidence=(produced[:160],)))
        outcome.baseline.append(BenchmarkResult(
            case_id=case_id, category=category, score=base_score,
            passed=base_score >= 0.5, evidence=(base_text[:160],)))

    outcome.cases = len(outcome.results)
    outcome.average = sum(r.score for r in outcome.results) / len(outcome.results)
    outcome.baseline_average = (
        sum(r.score for r in outcome.baseline) / len(outcome.baseline))
    baseline_by_category = {category: outcome.baseline_average}
    outcome.artifact = studio.promote(
        artifact, outcome.results, gate=gate or PromotionGate(),
        baseline_by_category=baseline_by_category)
    manifest = studio.save_manifest(outcome.artifact, outcome.results)
    outcome.manifest = str(manifest)
    if not outcome.promoted:
        outcome.notes.append(
            "gate rejected the adapter: average %.4f (baseline %.4f, "
            "regression budget is the gate's maximum_regression)"
            % (outcome.average, outcome.baseline_average))
    return outcome


__all__ = [
    "EvaluationOutcome",
    "answer_similarity",
    "evaluate_artifact",
    "greedy_answer",
    "held_out_records",
]
