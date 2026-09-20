"""Run a fine-tuning job honestly: preflight, train, report, never pretend.

A fine-tune needs a machine that can actually do it — a training stack (torch,
transformers, peft) and a registered trainer backend. The thin client cannot,
and neither can a sandbox with no GPU. So the job has explicit states, and
``BLOCKED`` always carries the exact missing requirement:

``BLOCKED``   nothing was attempted; ``blockers`` says what is missing
``READY``     dataset + plan + trainer are all present; training can start
``TRAINING``  a backend is running
``TRAINED``   a real artifact came back, marked trained by the backend
``FAILED``    the backend raised; the error is kept verbatim

The artifact is written through the Model Studio, so a run also produces a
provenance record (base model, method, dataset fingerprint) and can be
benchmarked and promoted by the existing gates.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from forge.models.model_studio import (
    ModelArtifact,
    ModelStudio,
    SpecializationProfile,
    TrainingBackend,
)


class JobState(str, Enum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    TRAINING = "TRAINING"
    TRAINED = "TRAINED"
    FAILED = "FAILED"


#: Import name -> human hint, checked in order during preflight.
_TRAINING_STACK = {
    "torch": "pip install torch (CPU build is enough for a small model)",
    "transformers": "pip install transformers",
    "peft": "pip install peft",
    "gguf": "pip install gguf",
    "tokenizers": "pip install tokenizers",
}


def _requirement_hint(module: str) -> str:
    """The pip line for a missing module, named exactly."""
    return _TRAINING_STACK.get(module, f"pip install {module}")


@dataclass
class FineTuneJob:
    """One fine-tuning attempt for one specialization."""

    specialization: str
    dataset_path: Path
    base_model: str
    records: Sequence[Mapping[str, Any]]
    studio: ModelStudio
    trainer: str = ""
    epochs: int = 1
    method: str = "lora"
    state: JobState = JobState.BLOCKED
    blockers: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    artifact: ModelArtifact | None = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    free_disk_gb: float = 0.0

    # -- preflight -----------------------------------------------------------

    def preflight(self, *, minimum_records: int = 8,
                  minimum_free_disk_gb: float = 2.0) -> JobState:
        """Decide whether training can start, and say exactly what is missing."""
        self.blockers = []
        self.requirements = []

        #: What must be installed depends on *which* trainer runs. A backend
        #: that never touches the HuggingFace stack (the GGUF LoRA trainer, or
        #: an operator's own backend) must not be blocked because torch is
        #: absent — that was a real bug: a registered, self-declared-available
        #: backend could never reach READY on a machine without torch.
        backend = (self.studio.trainer(self.trainer)
                   if self.trainer in self.studio.trainers() else None)
        if backend is not None:
            declared = tuple(getattr(backend, "requires", ()) or ())
            missing = [name for name in declared if _importable(name) is None]
            if missing:
                self.blockers.append(
                    "training stack is not installed: " + ", ".join(missing))
                self.requirements.extend(
                    _requirement_hint(name) for name in missing)
            elif not self._backend_available(backend):
                #: No stack module is missing, so the backend's own reason is
                #: reported instead of an invented requirement.
                self.blockers.append(
                    f"trainer {self.trainer!r} reports it cannot run here "
                    "(its available() check fails — e.g. no base model or no "
                    "GPU it requires)")
                self.requirements.append(
                    f"inspect the {self.trainer!r} trainer configuration")
        else:
            missing = [name for name in _TRAINING_STACK
                       if _importable(name) is None]
            if missing:
                self.blockers.append(
                    "training stack is not installed: " + ", ".join(missing))
                self.requirements.extend(
                    _requirement_hint(name) for name in missing)

        if not self.trainer:
            self.blockers.append(
                "no trainer backend was selected; register one with the Model "
                "Studio (e.g. the HuggingFace PEFT backend) and name it")
        elif self.trainer not in self.studio.trainers():
            self.blockers.append(
                f"trainer {self.trainer!r} is not registered with the studio "
                f"(registered: {self.studio.trainers() or 'none'})")
            self.requirements.append(
                "register a TrainingBackend, e.g. "
                "studio.register_trainer(HuggingFacePEFTBackend())")

        if len(self.records) < minimum_records:
            self.blockers.append(
                f"dataset has {len(self.records)} usable examples; "
                f"{minimum_records} are required to fine-tune at all")

        self.free_disk_gb = _free_disk_gb(self.dataset_path)
        if self.free_disk_gb and self.free_disk_gb < minimum_free_disk_gb:
            self.blockers.append(
                f"only {self.free_disk_gb:.1f} GB free; "
                f"{minimum_free_disk_gb:.1f} GB required for an adapter run")

        self.state = JobState.BLOCKED if self.blockers else JobState.READY
        return self.state

    @staticmethod
    def _backend_available(backend: Any) -> bool:
        """Whether the backend itself says it can run (never assumed)."""
        available = getattr(backend, "available", None)
        if not callable(available):
            #: A backend that does not implement the check is taken at its
            #: word: the trainer contract makes ``available`` optional.
            return True
        try:
            return bool(available())
        except Exception:                                     # noqa: BLE001
            return False

    # -- training ------------------------------------------------------------

    def run(self, *, minimum_records: int = 8) -> ModelArtifact | None:
        """Train when possible; otherwise stay BLOCKED with the reason."""
        if self.state not in (JobState.READY, JobState.TRAINING):
            if self.preflight(minimum_records=minimum_records) is not JobState.READY:
                return None

        capabilities = tuple(sorted({
            str(record.get("capability") or "")
            for record in self.records if record.get("capability")
        })) or ("text", "reasoning")
        profile = SpecializationProfile(
            name=self.specialization,
            description=f"{self.specialization} specialist adapter",
            domains=(self.specialization,),
            capabilities=capabilities,
        )
        plan = self.studio.make_training_plan(
            self.base_model, profile,
            dataset_size=len(self.records), method=self.method,
        )
        if self.epochs:
            plan = _with_epochs(plan, self.epochs)

        self.state = JobState.TRAINING
        self.started_at = time.time()
        try:
            artifact = self.studio.train(
                plan, list(self.records), trainer=self.trainer,
                output_name=f"{self.specialization}-{int(self.started_at)}",
            )
        except Exception as exc:                              # noqa: BLE001
            self.state = JobState.FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            self.finished_at = time.time()
            return None

        if artifact.provenance.trained is not True:
            # The studio already enforces this; belt and braces, because a
            # wrong "TRAINED" is worse than a missing one.
            self.state = JobState.FAILED
            self.error = "backend returned an artifact that is not marked trained"
            self.finished_at = time.time()
            return None

        self.artifact = artifact
        self.state = JobState.TRAINED
        self.finished_at = time.time()
        return artifact

    # -- reporting -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialization": self.specialization,
            "state": self.state.value,
            "trainer": self.trainer,
            "base_model": self.base_model,
            "dataset": str(self.dataset_path),
            "examples": len(self.records),
            "blockers": list(self.blockers),
            "requirements": list(self.requirements),
            "error": self.error,
            "free_disk_gb": round(self.free_disk_gb, 1),
            "duration_seconds": (round(self.finished_at - self.started_at, 1)
                                 if self.finished_at and self.started_at else 0.0),
            "artifact": self.artifact.to_dict() if self.artifact else None,
        }


def preflight(trainer: str = "", *, dataset: str | Path,
              specialization: str = "coder",
              base_model: str = "SmolLM2-135M-Instruct",
              minimum_records: int = 8) -> dict[str, Any]:
    """Standalone preflight: what would a fine-tune need on this machine?"""
    from forge.training.dataset import build_dataset

    studio = ModelStudio(root=".")
    path = Path(dataset)
    if str(path).endswith(".jsonl"):
        # Already a prepared dataset: read it as-is.
        bundle = _load_bundle(path, studio)
    else:
        # A fleet-run evidence artifact: build and validate a dataset from it.
        bundle = build_dataset([path], studio=studio,
                               output=f"{path}.finetune.jsonl")

    job = FineTuneJob(
        specialization=specialization,
        dataset_path=bundle.path,
        base_model=base_model,
        records=bundle.records,
        studio=studio,
        trainer=trainer,
    )
    job.preflight(minimum_records=minimum_records)
    report = job.to_dict()
    report["dataset_fingerprint"] = bundle.fingerprint
    report["dataset_by_role"] = bundle.by_role
    report["registered_trainers"] = studio.trainers()
    report["stack"] = {name: bool(_importable(name)) for name in _TRAINING_STACK}
    return report


def register_default_trainers(studio: ModelStudio, *, allow_network: bool = False,
                              base_model: str = "", steps: int = 30) -> list[str]:
    """Register the real training backends this machine can actually use.

    Two backends, in preference order:

    * ``gguf-lora`` — trains adapters against the local GGUF the runtime is
      serving. No download and no Hub access, which is what makes a fine-tune
      possible on a box that only has the weights it already serves.
    * ``huggingface-peft`` — the general backend, when the full stack is
      installed.

    Registering an unavailable backend would turn a clear preflight blocker
    ("the stack is missing") into a confusing failure later, so both are
    registered only when their own checks pass.
    """
    registered: list[str] = []
    try:
        from forge.training.gguf_lora import register_gguf_lora

        if register_gguf_lora(studio, base_model=base_model, steps=steps):
            registered.append("gguf-lora")
    except Exception:                                         # noqa: BLE001
        pass
    if all(_importable(name) for name in _TRAINING_STACK):
        from forge.models.hf_trainer import HuggingFacePEFTBackend

        studio.register_trainer(HuggingFacePEFTBackend(allow_network=allow_network))
        registered.append("huggingface-peft")
    return registered


# -- small helpers -------------------------------------------------------------

def _importable(name: str):
    import importlib.util

    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _free_disk_gb(path: Path) -> float:
    try:
        target = path if path.exists() else path.parent
        return shutil.disk_usage(str(target)).free / (1024 ** 3)
    except OSError:
        return 0.0


def _with_epochs(plan, epochs: int):
    from dataclasses import replace

    return replace(plan, epochs=max(1, int(epochs)))


def _load_bundle(path: Path, studio: ModelStudio):
    """Load a dataset previously written by :func:`build_dataset`."""
    import json

    from forge.training.dataset import DatasetBundle

    records: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    accepted, report = studio.validate_dataset(records)
    return DatasetBundle(
        path=path, records=list(accepted), report=report.to_dict(),
        fingerprint="", by_role={}, sources=[str(path)],
    )
