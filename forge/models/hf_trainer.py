"""Optional Hugging Face/PEFT training backend for Forge Model Studio.

The dependency stack is intentionally lazy-loaded: the core Forge package does
not require torch, transformers, peft, or bitsandbytes. A caller explicitly
registers this backend when those packages are installed on a trusted training
worker.

The backend never enables ``trust_remote_code`` and defaults to local-only model
loading. Network model downloads require an explicit ``allow_network=True``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from forge.models.model_studio import ModelArtifact, ModelProvenance, TrainingBackend, TrainingPlan


class HuggingFacePEFTBackend(TrainingBackend):
    name = "huggingface-peft"

    #: Modules this trainer imports; preflight names them exactly.
    requires: tuple[str, ...] = ("torch", "transformers", "peft")

    def __init__(self, *, allow_network: bool = False) -> None:
        self.allow_network = bool(allow_network)

    def _imports(self):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments
            try:
                from transformers import BitsAndBytesConfig
            except ImportError:
                BitsAndBytesConfig = None
            from peft import LoraConfig, TaskType, get_peft_model
            try:
                from transformers import DataCollatorForLanguageModeling
            except ImportError:
                DataCollatorForLanguageModeling = None
        except ImportError as exc:
            raise RuntimeError(
                "Hugging Face training requires torch, transformers and peft"
            ) from exc
        return (torch, AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments,
                BitsAndBytesConfig, LoraConfig, TaskType, get_peft_model,
                DataCollatorForLanguageModeling)

    def available(self) -> bool:
        try:
            self._imports()
            return True
        except RuntimeError:
            return False

    def train(self, plan: TrainingPlan, dataset: Sequence[Mapping[str, Any]],
              output_dir: Path) -> ModelArtifact:
        (torch, AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments,
         BitsAndBytesConfig, LoraConfig, TaskType, get_peft_model,
         DataCollatorForLanguageModeling) = self._imports()

        if not dataset:
            raise ValueError("training dataset is empty")
        output_dir.mkdir(parents=True, exist_ok=True)

        local_files_only = not self.allow_network
        tokenizer = AutoTokenizer.from_pretrained(
            plan.base_model,
            local_files_only=local_files_only,
            trust_remote_code=False,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        quant_config = None
        if plan.method == "qlora" and plan.quantization in ("4bit", "8bit"):
            if BitsAndBytesConfig is None:
                raise RuntimeError("QLoRA requires transformers BitsAndBytesConfig and bitsandbytes")
            if not torch.cuda.is_available():
                raise RuntimeError("QLoRA training requires a CUDA-capable training worker")
            if plan.quantization == "4bit":
                quant_config = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                )
            else:
                quant_config = BitsAndBytesConfig(load_in_8bit=True)

        model_kwargs = {
            "local_files_only": local_files_only,
            "trust_remote_code": False,
        }
        if quant_config is not None:
            model_kwargs["quantization_config"] = quant_config
            model_kwargs["device_map"] = "auto"
        model = AutoModelForCausalLM.from_pretrained(plan.base_model, **model_kwargs)

        if plan.method in ("lora", "qlora"):
            if plan.quantization != "none" and hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()
            lora = LoraConfig(
                r=plan.rank,
                lora_alpha=plan.alpha,
                lora_dropout=plan.dropout,
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
                task_type=TaskType.CAUSAL_LM,
                bias="none",
            )
            model = get_peft_model(model, lora)

        texts = []
        for record in dataset:
            instruction = str(record.get("instruction", ""))
            context = str(record.get("context", ""))
            output = str(record.get("output", ""))
            if not instruction or not output:
                continue
            system = plan.profile.system_instruction.strip()
            prompt = ""
            if system:
                prompt += "### System\n" + system + "\n\n"
            prompt += "### Instruction\n" + instruction + "\n"
            if context:
                prompt += "\n### Context\n" + context + "\n"
            prompt += "\n### Response\n" + output
            texts.append(prompt)
        if not texts:
            raise ValueError("dataset contains no trainable records")

        encoded = [
            tokenizer(
                text,
                truncation=True,
                max_length=plan.max_seq_length,
                padding=False,
            )
            for text in texts
        ]
        for item in encoded:
            item["labels"] = list(item["input_ids"])

        class _Dataset(torch.utils.data.Dataset):
            def __len__(self):
                return len(encoded)

            def __getitem__(self, index):
                return encoded[index]

        collator = None
        if DataCollatorForLanguageModeling is not None:
            collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

        train_args = TrainingArguments(
            output_dir=str(output_dir),
            num_train_epochs=plan.epochs,
            learning_rate=plan.learning_rate,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=plan.gradient_accumulation,
            gradient_checkpointing=plan.gradient_checkpointing,
            logging_strategy="no",
            save_strategy="epoch",
            report_to=[],
            remove_unused_columns=False,
            fp16=bool(torch.cuda.is_available()),
        )
        trainer = Trainer(
            model=model,
            args=train_args,
            train_dataset=_Dataset(),
            tokenizer=tokenizer,
            data_collator=collator,
        )
        trainer.train()

        adapter_path = output_dir / "adapter"
        adapter_path.mkdir(parents=True, exist_ok=True)
        if plan.method in ("lora", "qlora"):
            model.save_pretrained(str(adapter_path))
        else:
            model.save_pretrained(str(adapter_path), safe_serialization=True)
        tokenizer.save_pretrained(str(adapter_path))

        dataset_fingerprint = __import__(
            "forge.models.model_studio", fromlist=["ModelStudio"]
        ).ModelStudio(output_dir.parent.parent.parent).fingerprint_dataset(list(dataset))
        artifact_id = "model-" + __import__("hashlib").sha256(
            (plan.base_model + "\0" + plan.profile.name + "\0" + dataset_fingerprint).encode("utf-8")
        ).hexdigest()[:24]
        provenance = ModelProvenance(
            artifact_id=artifact_id,
            base_model=plan.base_model,
            specialization=plan.profile.name,
            method=plan.method,
            quantization=plan.quantization,
            dataset_fingerprint=dataset_fingerprint,
            trainer=self.name,
            trained=True,
            created_at=time.time(),
        )
        return ModelArtifact(
            id=artifact_id,
            path=str(adapter_path),
            provenance=provenance,
            status="candidate",
        )


__all__ = ["HuggingFacePEFTBackend"]
