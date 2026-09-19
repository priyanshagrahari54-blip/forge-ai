"""Fine-tuning pipeline for Forge's own specialists.

Forge already records what its specialists actually did: every fleet run writes
per-specialist records with the role, the required capability, the prompt and
the model's real answer. That is exactly the raw material a specialist adapter
needs — this package turns those traces into a validated dataset, plans the
training, and runs it through the existing :class:`~forge.models.model_studio.ModelStudio`
control plane (trainer registry, benchmark, promotion gate, manifest).

Honesty rules, same as everywhere else in Forge:

* a job reports ``BLOCKED`` with the missing requirement when the training
  stack or a trainer is absent — it never reports ``TRAINED`` without a real
  artifact from a registered backend;
* dataset validation is the studio's own (it rejects secret-bearing records),
  so a fingerprint can be trusted;
* nothing here downloads a base model or calls the network by itself.
"""
from forge.training.dataset import (
    build_dataset,
    export_instruction_records,
    records_from_evidence,
)
from forge.training.job import FineTuneJob, JobState, preflight

__all__ = [
    "FineTuneJob",
    "JobState",
    "build_dataset",
    "export_instruction_records",
    "preflight",
    "records_from_evidence",
]
