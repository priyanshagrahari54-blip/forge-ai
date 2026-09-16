"""Forge Model Studio: safe model customization and upgrade orchestration.

This module deliberately separates *model customization planning* from model
execution. It provides a dependency-light, reproducible control plane for:

* validating and normalizing instruction/SFT datasets;
* removing malformed/duplicate/secret-bearing samples;
* generating deterministic train/validation splits;
* defining specialization objectives and capability profiles;
* producing LoRA/QLoRA/adapter/quantization plans without pretending that
  weights were trained when no training backend actually ran;
* evaluating candidates with repeatable benchmark cases and gating promotion;
* recording provenance so the inference fabric knows exactly what artifact it
  is serving.

Actual weight updates are delegated to an explicitly registered trainer. The
base Forge install remains stdlib-only at this layer, so the platform still
works on the Python 3.8 / Windows 7 compatibility target.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

__all__ = [
    "BenchmarkCase",
    "BenchmarkResult",
    "DatasetIssue",
    "DatasetReport",
    "ModelArtifact",
    "ModelProvenance",
    "ModelStudio",
    "PromotionGate",
    "SpecializationProfile",
    "TrainingBackend",
    "TrainingPlan",
    "build_sft_record",
    "stable_split",
]


MAX_RECORD_CHARS = 200_000
MAX_FIELD_CHARS = 80_000
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk|pk|rk|ak)-[a-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|password|passwd|token|authorization)\s*[:=]"),
)


def _secret_like(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SECRET_PATTERNS)


def _clean_text(value: Any, *, max_chars: int = MAX_FIELD_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "").strip()
    if len(text) > max_chars:
        raise ValueError("text field exceeds safety bound")
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class DatasetIssue:
    index: int
    code: str
    message: str
    fatal: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetReport:
    accepted: int
    rejected: int
    duplicates_removed: int
    secret_bearing: int
    issues: List[DatasetIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.fatal for issue in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["issues"] = [issue.to_dict() for issue in self.issues]
        data["ok"] = self.ok
        return data


@dataclass(frozen=True)
class SpecializationProfile:
    """What a customized model is supposed to become."""

    name: str
    description: str
    domains: Tuple[str, ...]
    capabilities: Tuple[str, ...] = ("text", "reasoning")
    preferred_tasks: Tuple[str, ...] = ("coding", "debugging", "review")
    system_instruction: str = ""
    max_context_tokens: int = 32768
    max_output_tokens: int = 8192
    quality_floor: float = 0.80

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name cannot be empty")
        if not 1 <= self.max_context_tokens <= 2_000_000:
            raise ValueError("max_context_tokens outside supported bound")
        if not 1 <= self.max_output_tokens <= 200_000:
            raise ValueError("max_output_tokens outside supported bound")
        if not 0.0 <= self.quality_floor <= 1.0:
            raise ValueError("quality_floor must be within [0,1]")


@dataclass(frozen=True)
class TrainingPlan:
    base_model: str
    profile: SpecializationProfile
    method: str = "qlora"
    quantization: str = "4bit"
    rank: int = 16
    alpha: int = 32
    dropout: float = 0.05
    learning_rate: float = 2e-4
    epochs: int = 2
    max_seq_length: int = 8192
    gradient_checkpointing: bool = True
    gradient_accumulation: int = 16
    expected_train_examples: int = 0

    def __post_init__(self) -> None:
        if self.method not in {"lora", "qlora", "full", "prompt"}:
            raise ValueError("unsupported training method")
        if self.quantization not in {"none", "4bit", "8bit"}:
            raise ValueError("unsupported quantization")
        if not 1 <= self.rank <= 512:
            raise ValueError("rank outside bound")
        if not 1 <= self.alpha <= 2048:
            raise ValueError("alpha outside bound")
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout outside bound")
        if not 0.0 < self.learning_rate <= 1.0:
            raise ValueError("learning_rate outside bound")
        if not 1 <= self.epochs <= 100:
            raise ValueError("epochs outside bound")
        if not 128 <= self.max_seq_length <= 1_000_000:
            raise ValueError("max_seq_length outside bound")
        if self.gradient_accumulation < 1:
            raise ValueError("gradient_accumulation must be positive")
        if self.expected_train_examples < 0:
            raise ValueError("expected_train_examples cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    category: str
    prompt: str
    expected_signals: Tuple[str, ...] = ()
    min_score: float = 0.70


@dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    category: str
    score: float
    passed: bool
    evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionGate:
    minimum_average: float = 0.80
    minimum_category: float = 0.70
    maximum_regression: float = 0.05
    require_all_critical: bool = True


@dataclass(frozen=True)
class ModelProvenance:
    artifact_id: str
    base_model: str
    specialization: str
    method: str
    quantization: str
    dataset_fingerprint: str
    trainer: str
    trained: bool
    parent_artifact: str = ""
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelArtifact:
    """A promoted model artifact or a training-plan placeholder."""

    id: str
    path: str
    provenance: ModelProvenance
    status: str = "candidate"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "status": self.status,
            "provenance": self.provenance.to_dict(),
        }


class TrainingBackend:
    """Adapter contract for actual model-weight training."""

    name = "abstract"

    def available(self) -> bool:
        return False

    def train(self, plan: TrainingPlan, dataset: Sequence[Mapping[str, Any]],
              output_dir: Path) -> ModelArtifact:
        raise NotImplementedError


class ModelStudio:
    """Dataset, training-plan, benchmark and promotion control plane."""

    def __init__(self, root: str | Path = ".", *, seed: str = "forge-model-studio-v1") -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / ".forge" / "models"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self._trainers: Dict[str, TrainingBackend] = {}

    def register_trainer(self, trainer: TrainingBackend) -> None:
        name = _clean_text(getattr(trainer, "name", ""), max_chars=128)
        if not name:
            raise ValueError("trainer name is required")
        self._trainers[name] = trainer

    def trainers(self) -> List[str]:
        return sorted(self._trainers)

    def validate_dataset(self, records: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, str]], DatasetReport]:
        accepted: List[Dict[str, str]] = []
        issues: List[DatasetIssue] = []
        seen: set[str] = set()
        duplicate_count = 0
        secret_count = 0
        rejected = 0

        for index, raw in enumerate(records):
            if not isinstance(raw, Mapping):
                rejected += 1
                issues.append(DatasetIssue(index, "NOT_OBJECT", "record must be an object", True))
                continue
            try:
                instruction = _clean_text(raw.get("instruction", raw.get("prompt", "")))
                output = _clean_text(raw.get("output", raw.get("response", "")))
                context = _clean_text(raw.get("context", ""))
            except ValueError as exc:
                rejected += 1
                issues.append(DatasetIssue(index, "FIELD_BOUND", str(exc), True))
                continue
            if not instruction or not output:
                rejected += 1
                issues.append(DatasetIssue(index, "EMPTY", "instruction and output are required"))
                continue
            canonical = _canonical_json({"instruction": instruction, "output": output, "context": context})
            if len(canonical) > MAX_RECORD_CHARS:
                rejected += 1
                issues.append(DatasetIssue(index, "RECORD_BOUND", "record exceeds safety bound"))
                continue
            if _secret_like(canonical):
                rejected += 1
                secret_count += 1
                issues.append(DatasetIssue(index, "SECRET", "secret-bearing sample rejected"))
                continue
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if fingerprint in seen:
                duplicate_count += 1
                issues.append(DatasetIssue(index, "DUPLICATE", "duplicate sample removed"))
                continue
            seen.add(fingerprint)
            accepted.append({
                "instruction": instruction,
                "output": output,
                "context": context,
            })

        return accepted, DatasetReport(
            accepted=len(accepted),
            rejected=rejected,
            duplicates_removed=duplicate_count,
            secret_bearing=secret_count,
            issues=issues,
        )

    def fingerprint_dataset(self, records: Sequence[Mapping[str, Any]]) -> str:
        canonical = [_canonical_json(record) for record in records]
        payload = "\n".join(canonical).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def split(self, records: Sequence[Mapping[str, Any]], validation_ratio: float = 0.1) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
        return stable_split(records, validation_ratio=validation_ratio, seed=self.seed)

    def make_training_plan(self, base_model: str, profile: SpecializationProfile,
                           *, dataset_size: int = 0, method: Optional[str] = None,
                           quantization: Optional[str] = None) -> TrainingPlan:
        chosen_method = method or ("qlora" if profile.max_context_tokens >= 32768 else "lora")
        chosen_quant = quantization or ("4bit" if chosen_method == "qlora" else "none")
        # Conservative defaults for a memory-constrained client, while keeping
        # the plan scalable on a server with a stronger GPU.
        rank = 16 if dataset_size < 100_000 else 32
        accumulation = 16 if profile.max_context_tokens <= 32768 else 32
        return TrainingPlan(
            base_model=_clean_text(base_model, max_chars=256),
            profile=profile,
            method=chosen_method,
            quantization=chosen_quant,
            rank=rank,
            alpha=rank * 2,
            gradient_accumulation=accumulation,
            expected_train_examples=dataset_size,
            max_seq_length=min(profile.max_context_tokens, 131072),
        )

    def train(self, plan: TrainingPlan, records: Sequence[Mapping[str, Any]],
              *, trainer: str, output_name: str) -> ModelArtifact:
        backend = self._trainers.get(trainer)
        if backend is None:
            raise ValueError("trainer %r is not registered" % trainer)
        if not backend.available():
            raise RuntimeError("training backend %r is unavailable" % trainer)
        output_dir = self.directory / _clean_text(output_name, max_chars=128)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = backend.train(plan, records, output_dir)
        if artifact.provenance.trained is not True:
            raise RuntimeError("training backend returned an artifact not marked as trained")
        return artifact

    def evaluate(self, runner: Callable[[str], str], cases: Sequence[BenchmarkCase]) -> List[BenchmarkResult]:
        results: List[BenchmarkResult] = []
        for case in cases:
            answer = _clean_text(runner(case.prompt), max_chars=MAX_RECORD_CHARS)
            lowered = answer.lower()
            hits = tuple(signal for signal in case.expected_signals if signal.lower() in lowered)
            if case.expected_signals:
                score = len(hits) / float(len(case.expected_signals))
            else:
                score = 1.0 if answer else 0.0
            score = max(0.0, min(1.0, score))
            results.append(BenchmarkResult(
                case_id=case.id,
                category=case.category,
                score=score,
                passed=score >= case.min_score,
                evidence=hits,
            ))
        return results

    def promote(self, artifact: ModelArtifact, results: Sequence[BenchmarkResult],
                *, gate: PromotionGate = PromotionGate(), baseline_by_category: Optional[Mapping[str, float]] = None) -> ModelArtifact:
        if not results:
            raise ValueError("promotion requires benchmark results")
        average = sum(result.score for result in results) / float(len(results))
        by_category: Dict[str, List[float]] = {}
        for result in results:
            by_category.setdefault(result.category, []).append(result.score)
        category_averages = {key: sum(values) / float(len(values)) for key, values in by_category.items()}
        critical_fail = gate.require_all_critical and any(not result.passed for result in results)
        category_fail = any(value < gate.minimum_category for value in category_averages.values())
        regression_fail = False
        if baseline_by_category:
            regression_fail = any(
                baseline_by_category.get(category, score) - score > gate.maximum_regression
                for category, score in category_averages.items()
            )
        passed = average >= gate.minimum_average and not critical_fail and not category_fail and not regression_fail
        status = "promoted" if passed else "rejected"
        return ModelArtifact(
            id=artifact.id,
            path=artifact.path,
            provenance=artifact.provenance,
            status=status,
        )

    def save_manifest(self, artifact: ModelArtifact, results: Sequence[BenchmarkResult]) -> Path:
        payload = {
            "artifact": artifact.to_dict(),
            "benchmarks": [asdict(result) for result in results],
        }
        path = self.directory / (artifact.id + ".manifest.json")
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return path


def stable_split(records: Sequence[Mapping[str, Any]], *, validation_ratio: float = 0.1,
                 seed: str = "forge-model-studio-v1") -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Deterministic hash split: reproducible across processes and machines."""
    if not 0.0 < validation_ratio < 1.0:
        raise ValueError("validation_ratio must be between 0 and 1")
    cutoff = int(validation_ratio * 10_000)
    train: List[Mapping[str, Any]] = []
    validation: List[Mapping[str, Any]] = []
    for record in records:
        key = hashlib.sha256((seed + "\0" + _canonical_json(record)).encode("utf-8")).hexdigest()
        bucket = int(key[:8], 16) % 10_000
        (validation if bucket < cutoff else train).append(record)
    return train, validation


def build_sft_record(instruction: str, output: str, *, context: str = "") -> Dict[str, str]:
    """Normalize one instruction-tuning sample for trainer adapters."""
    instruction = _clean_text(instruction)
    output = _clean_text(output)
    context = _clean_text(context)
    if not instruction or not output:
        raise ValueError("instruction and output are required")
    if _secret_like(instruction + "\n" + context + "\n" + output):
        raise ValueError("secret-bearing training sample refused")
    return {"instruction": instruction, "context": context, "output": output}
