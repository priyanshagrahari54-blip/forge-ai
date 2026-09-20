"""A real LoRA trainer for the GGUF the runtime is actually serving.

Why this exists: fine-tuning needs the *deployed* weights. This backend reads
the same GGUF file llama-server serves (dequantizing it with ``gguf.quants``),
trains LoRA adapters on Forge's own evidence data with torch on CPU, and writes
two artifacts:

* ``adapter_model.safetensors`` — the adapter in a portable, inspectable form;
* ``adapter.gguf`` — the same adapter in llama.cpp's adapter format, so
  ``llama-server --lora adapter.gguf`` can serve the fine-tuned specialist.

``available()`` is evidence-based: torch, gguf and tokenizers must import *and*
the base model file must resolve. Nothing here downloads anything.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from forge.models.model_studio import (
    ModelArtifact,
    ModelProvenance,
    TrainingBackend,
    TrainingPlan,
)
from forge.training.llama_lora import (
    LlamaLoraModel,
    LoRAConfig,
    evaluate_loss,
    train_lora,
)

#: Where a base GGUF may live when the plan names a model rather than a path.
DEFAULT_MODEL_DIRS = (
    Path("/home/user/models"),
    Path(".forge/models"),
    Path("models"),
)

#: ChatML, exactly as this model's own GGUF template specifies.
CHAT_SYSTEM = "You are a Forge engineering specialist."
CHATML_SYSTEM = "<|im_start|>system\n{system}<|im_end|>\n"
CHATML_USER = "<|im_start|>user\n{prompt}<|im_end|>\n"
CHATML_ASSISTANT = "<|im_start|>assistant\n{response}<|im_end|>\n"

SPECIAL_TOKENS = (
    "<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|end_of_text|>",
    "<fim_prefix>", "<fim_middle>", "<fim_suffix>", "<fim_pad>",
    "<repo_name>", "<file_sep>", "<|file_sep|>",
)


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def resolve_base_model(base_model: str = "") -> Path | None:
    """Resolve a plan's base model to a GGUF file on this machine.

    An empty name is not "no model": it means the operator has not named one,
    so the explicit override (``FORGE_BASE_GGUF``) and then the single GGUF
    sitting in the conventional directories are used. A machine that hosts
    exactly one model needs no configuration to fine-tune it.
    """
    candidate = Path(base_model) if base_model else None
    if candidate is not None and candidate.is_file() and \
            candidate.suffix == ".gguf":
        return candidate
    env = os.environ.get("FORGE_BASE_GGUF", "")
    if env and Path(env).is_file():
        return Path(env)
    if not base_model:
        found = [entry for directory in DEFAULT_MODEL_DIRS
                 if directory.is_dir()
                 for entry in sorted(directory.glob("*.gguf"))]
        return found[0] if len(found) == 1 else None
    stem = candidate.stem or base_model
    for directory in DEFAULT_MODEL_DIRS:
        if not directory.is_dir():
            continue
        direct = directory / f"{stem}.gguf"
        if direct.is_file():
            return direct
        # A name like "SmolLM2-135M-Instruct" matches
        # "SmolLM2-135M-Instruct.Q4_1.gguf" — same model, quantized file name.
        for entry in sorted(directory.glob("*.gguf")):
            if entry.name.lower().startswith(stem.lower()):
                return entry
    return None


def build_sequences(gguf, records: Sequence[Mapping[str, Any]], *,
                    max_length: int = 256) -> tuple[list[list[int]], list[list[int]]]:
    """ChatML-encode the dataset; the last 10% is held out for evaluation."""
    tokenizer = gguf.tokenizer()
    try:
        tokenizer.add_special_tokens(list(SPECIAL_TOKENS))
    except Exception:                                         # noqa: BLE001
        pass

    sequences: list[list[int]] = []
    for record in records:
        prompt = str(record.get("instruction") or record.get("prompt") or "").strip()
        response = str(record.get("output") or record.get("response") or "").strip()
        if not prompt or not response:
            continue
        text = (CHATML_SYSTEM.format(system=CHAT_SYSTEM)
                + CHATML_USER.format(prompt=prompt)
                + CHATML_ASSISTANT.format(response=response))
        ids = gguf.encode(text)[:max_length]
        if len(ids) > 4:
            sequences.append(ids)
    split = max(1, int(len(sequences) * 0.9))
    return sequences[:split], sequences[split:] or sequences[:1]


class GgufLoRATrainer(TrainingBackend):
    """Train LoRA adapters against a local GGUF, on CPU, with real weights."""

    name = "gguf-lora"

    #: Modules this trainer imports; preflight names them exactly.
    requires: tuple[str, ...] = ("torch", "gguf", "tokenizers")

    def __init__(self, base_model: str | Path | None = None, *,
                 steps: int = 30, learning_rate: float = 3e-3,
                 rank: int = 8, alpha: int = 16, batch_size: int = 2,
                 max_length: int = 256, seed: int = 0) -> None:
        self.base_model = str(base_model) if base_model else ""
        self.steps = max(1, int(steps))
        self.learning_rate = float(learning_rate)
        self.rank = int(rank)
        self.alpha = int(alpha)
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(32, int(max_length))
        self.seed = int(seed)

    # -- contract ------------------------------------------------------------

    def available(self) -> bool:
        if not all(_importable(name) for name in ("torch", "gguf", "tokenizers")):
            return False
        return self.resolve_base() is not None

    def resolve_base(self) -> Path | None:
        return resolve_base_model(self.base_model)

    def train(self, plan: TrainingPlan, dataset: Sequence[Mapping[str, Any]],
              output_dir: Path) -> ModelArtifact:
        from forge.training.gguf_model import GgufLlama

        base = resolve_base_model(self.base_model or plan.base_model)
        if base is None:
            raise RuntimeError(
                "no base GGUF found: set FORGE_BASE_GGUF or place the model in "
                f"one of {[str(p) for p in DEFAULT_MODEL_DIRS]}")
        if not dataset:
            raise RuntimeError("no training records were supplied")

        gguf = GgufLlama(base)
        train_sequences, eval_sequences = build_sequences(
            gguf, dataset, max_length=self.max_length)
        if not train_sequences:
            raise RuntimeError("no usable sequences after tokenization")

        config = LoRAConfig(targets=("attn_q", "attn_v"), rank=self.rank,
                            alpha=self.alpha)
        model = LlamaLoraModel(config=config, seed=self.seed, gguf=gguf)

        started = time.time()
        result = train_lora(
            model, train_sequences, steps=self.steps,
            learning_rate=self.learning_rate, batch_size=self.batch_size,
            log_every=max(1, self.steps // 6), seed=self.seed,
        )
        held_out = evaluate_loss(model, eval_sequences, batches=2)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = self._save_safetensors(model, output_dir)
        try:
            adapter = self._save_gguf_adapter(model, output_dir, config, plan)
        except Exception as exc:                              # noqa: BLE001
            # The loss curve is real evidence and must not be lost because an
            # export format failed; the artifact is still refused, so no caller
            # can mistake a half-written adapter for a trainable one.
            self._write_metadata(output_dir, base, plan, config, result,
                                 held_out, len(train_sequences),
                                 len(eval_sequences), checkpoint, None)
            raise RuntimeError(
                f"trained ({result.initial_loss:.3f} -> {result.final_loss:.3f} "
                f"loss, {result.seconds:.0f}s) but the llama.cpp adapter export "
                f"failed: {type(exc).__name__}: {exc}") from exc

        self._write_metadata(output_dir, base, plan, config, result, held_out,
                             len(train_sequences), len(eval_sequences),
                             checkpoint, adapter)

        return ModelArtifact(
            id=f"{plan.profile.name}-lora-{int(started)}",
            path=str(output_dir),
            provenance=ModelProvenance(
                artifact_id=f"{plan.profile.name}-lora-{int(started)}",
                base_model=str(base),
                specialization=plan.profile.name,
                method="lora",
                quantization="gguf-dequantized-fp32",
                dataset_fingerprint=str(
                    plan.expected_train_examples) + ":" + str(len(train_sequences)),
                trainer=self.name,
                #: Set only because weights really were trained: the loss curve
                #: and both artifacts are on disk and reported.
                trained=True,
                created_at=started,
            ),
            status="candidate",
        )

    # -- artifacts -----------------------------------------------------------

    def _write_metadata(self, output_dir: Path, base: Path, plan: TrainingPlan,
                        config: LoRAConfig, result, held_out: float,
                        train_count: int, eval_count: int,
                        checkpoint: Path, adapter: Path | None) -> Path:
        """The training record: loss curve, artifact names, exact base file."""
        metadata_path = Path(output_dir) / "training.json"
        metadata_path.write_text(json.dumps({
            "base_model": str(base),
            "base_model_bytes": base.stat().st_size,
            "specialization": plan.profile.name,
            "method": "lora",
            "rank": config.rank,
            "alpha": config.alpha,
            "targets": list(config.targets),
            "steps": result.steps,
            "learning_rate": self.learning_rate,
            "train_sequences": train_count,
            "eval_sequences": eval_count,
            "initial_loss": round(result.initial_loss, 4),
            "final_loss": round(result.final_loss, 4),
            "held_out_loss": round(held_out, 4),
            "seconds": round(result.seconds, 2),
            "trainable_parameters": result.trainable_parameters,
            "history": [{"step": record.step, "loss": round(record.loss, 4)}
                        for record in result.history],
            "safetensors": checkpoint.name,
            "gguf_adapter": adapter.name if adapter else "",
            "servable": adapter is not None,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return metadata_path


    def _save_safetensors(self, model: LlamaLoraModel, output_dir: Path) -> Path:
        from safetensors.torch import save_file

        target = output_dir / "adapter_model.safetensors"
        save_file({name: tensor.detach().contiguous()
                   for name, tensor in model.params.items()}, str(target))
        return target

    def _save_gguf_adapter(self, model: LlamaLoraModel, output_dir: Path,
                           config: LoRAConfig, plan: TrainingPlan) -> Path:
        """Write llama.cpp's adapter format so ``llama-server --lora`` can use it."""
        import numpy as np
        from gguf import GGUFWriter
        from gguf.constants import GGMLQuantizationType

        target = output_dir / "adapter.gguf"
        writer = GGUFWriter(str(target), "llama")
        writer.add_string("general.type", "adapter")
        writer.add_string("adapter.type", "lora")
        writer.add_float32("adapter.lora_alpha", float(config.alpha))
        writer.add_string("adapter.base_model", plan.base_model)
        writer.add_string("adapter.specialization", plan.profile.name)
        #: llama.cpp pairs these as "<base tensor>.lora_a" / ".lora_b".
        for name, tensor in model.params.items():
            # The GGUF writer takes numpy arrays and needs the dtype stated
            # explicitly; F32 is what an adapter for a quantized base wants.
            array = np.ascontiguousarray(
                tensor.detach().to("cpu").to(model.torch.float32).numpy())
            writer.add_tensor(name, array,
                              raw_dtype=GGMLQuantizationType.F32)
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()
        return target


def register_gguf_lora(studio, *, base_model: str = "", steps: int = 30,
                       **kwargs) -> str | None:
    """Register this backend when the machine can actually run it."""
    trainer = GgufLoRATrainer(base_model or None, steps=steps, **kwargs)
    if not trainer.available():
        return None
    studio.register_trainer(trainer)
    return trainer.name
