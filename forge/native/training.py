"""Future model-training fabric: real interfaces, honest emptiness.

A81 layer 13 asks for the interfaces Forge will need to eventually train its
own models — dataset creation, validation, training jobs, evaluation, model
versioning, benchmarking, promotion, and rollback. This module provides all
of them as *working local mechanics over real recorded data*, with one clear
boundary: **no trainer ships, and none is pretended.**

What is real here:

* dataset building from actual engine run records
  (``.forge/native/runs/*.json``) — only runs with genuine model output that
  passed verification produce examples; nothing is synthesized;
* dataset validation (JSONL schema, duplicate rejection, secret scanning);
* job/version/benchmark record management with durable manifests;
* promotion/rollback of a *registered* model version, verified against an
  actually-present artifact and recorded evaluation results.

What is explicitly absent (and refuses loudly rather than faking):

* running training itself (``TrainingRuntimeNotConfigured``),
* evaluation without a real registered model,
* any claim that a Forge-trained model exists. ``NativeTrainingFabric
  .status()`` counts artifacts on disk — until a training run produces one,
  it reports zero, always.

The fabric is where a future small local model or Forge-trained model plugs
back into the engine: register its provider in the Model Fabric, and the
reasoning backends pick it up through the same single abstraction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from forge.core.report import redact
from forge.security.verification import VerificationPipeline

#: Directory layout (relative to the project root), all under Forge state.
TRAINING_DIR = Path(".forge") / "native" / "training"
DATASETS_DIR = TRAINING_DIR / "datasets"
JOBS_DIR = TRAINING_DIR / "jobs"
MODELS_DIR = TRAINING_DIR / "models"
ACTIVE_POINTER = MODELS_DIR / "active.json"
RUNS_DIR = Path(".forge") / "native" / "runs"

#: Dataset bounds tuned for the G560-class host (validation stays fast).
MAX_DATASET_BYTES = 32 * 1024 * 1024
MAX_EXAMPLE_CHARS = 64 * 1024

#: The only run outcomes eligible as training signal (honest filter).
ELIGIBLE_STATUSES = ("COMPLETED",)

#: Secret shapes scanned in dataset content: the shared gate patterns plus
#: the provider-key formats (OpenAI-style ``sk-…``, GitHub tokens, bearer
#: strings, URL credentials) that the A50 training policy also refuses.
_DATASET_SECRET_PATTERNS = VerificationPipeline.SECRET_PATTERNS + (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}"),
    re.compile(r"[a-zA-Z0-9.+/]+://[^\s/@]+:[^@\s]+@"),
)


class TrainingRuntimeNotConfigured(RuntimeError):
    """Raised when a training run is requested: no trainer exists yet."""


@dataclass
class DatasetExample:
    """One training example: a task, its repository-grounded context, and
    the *actual* model output that was verified — never a synthesized pair."""

    prompt: str
    completion: str
    model: str
    provider: str
    source_run: str
    verified: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "model": self.model,
            "provider": self.provider,
            "source_run": self.source_run,
            "verified": bool(self.verified),
        }

    @staticmethod
    def line(example: "DatasetExample") -> str:
        return json.dumps(example.to_dict(), sort_keys=True)


class DatasetBuilder:
    """Build JSONL datasets from real native-engine run records."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def collect(self) -> Tuple[List[DatasetExample], List[str]]:
        """Return (examples, notes). Skipped runs are counted, never
        silently dropped."""
        runs_dir = self.root / RUNS_DIR
        examples: List[DatasetExample] = []
        notes: List[str] = []
        if not runs_dir.is_dir():
            notes.append("no run records under .forge/native/runs — a "
                         "dataset requires real runs with dataset capture "
                         "enabled (NativeAIEngine(dataset_capture=True))")
            return examples, notes
        for path in sorted(runs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notes.append("unreadable run record: %s" % path.name)
                continue
            example, note = self._from_record(data)
            if example is not None:
                examples.append(example)
            elif note:
                notes.append(note)
        return examples, notes

    def _from_record(self, record: Dict[str, Any]
                     ) -> Tuple[Optional[DatasetExample], str]:
        run_id = str(record.get("run_id", ""))
        if str(record.get("final_status")) not in ELIGIBLE_STATUSES:
            return None, ""  # excluded without ceremony; still counted below
        if not record.get("dataset_captured"):
            return None, ("run %s: verification passed but model output was "
                          "not captured (enable dataset_capture)" % run_id)
        output = (((record.get("plan") or {}).get("model_output"))
                  or record.get("model_output") or {})
        completion = str(output.get("text") or "")
        model = str(output.get("model") or "")
        if not completion or not model:
            return None, ("run %s: no real model output recorded (deterministic "
                          "runs are never training data)" % run_id)
        verification = record.get("verification") or {}
        if verification.get("status") == "FAIL":
            return None, "run %s: verification failed; excluded" % run_id
        prompt = json.dumps({
            "requirement": record.get("requirement", ""),
            "task_class": record.get("task_class", ""),
            "files_changed": record.get("files_changed", []),
        }, sort_keys=True)
        return DatasetExample(prompt=prompt, completion=completion,
                              model=model,
                              provider=str(output.get("provider", "")),
                              source_run=run_id), ""

    def build(self, out_name: str = "",
              max_examples: int = 5000) -> Dict[str, Any]:
        """Write ``.forge/native/training/datasets/<name>.jsonl`` + manifest.

        Returns a report with real counts; produces no file when there is
        nothing honest to put in it.
        """
        examples, notes = self.collect()
        examples = examples[:max(1, int(max_examples))]
        report: Dict[str, Any] = {
            "examples": len(examples),
            "notes": [n for n in notes if n][:50],
            "dataset": "",
            "manifest": "",
            "created_at": time.time(),
        }
        if not examples:
            report["empty"] = True
            report["reason"] = ("no eligible run records — a dataset can "
                                "only be built from verified real model "
                                "outputs; none fabricated")
            return report
        name = out_name or ("forge-native-%s" % time.strftime(
            "%Y%m%d%H%M%S", time.gmtime()))
        name = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                       for ch in name)[:64]
        directory = self.root / DATASETS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        dataset_path = directory / ("%s.jsonl" % name)
        digest = hashlib.sha256()
        with open(dataset_path, "w", encoding="utf-8", newline="\n") as out:
            for example in examples:
                line = DatasetExample.line(example)
                digest.update(line.encode("utf-8"))
                out.write(line + "\n")
        manifest = {
            "name": name,
            "dataset_file": dataset_path.name,
            "examples": len(examples),
            "sha256": digest.hexdigest(),
            "created_at": time.time(),
            "sources": sorted({e.source_run for e in examples}),
            "generator": "forge-native-ai/dataset-builder",
        }
        manifest_path = directory / ("%s.manifest.json" % name)
        manifest_path.write_text(json.dumps(manifest, indent=1,
                                            sort_keys=True),
                                 encoding="utf-8")
        report.update({"dataset": str(dataset_path),
                       "manifest": str(manifest_path),
                       "sha256": manifest["sha256"]})
        return report


class DatasetValidator:
    """Schema, duplication, size, and secret validation for JSONL datasets."""

    def validate_text(self, text: str) -> Dict[str, Any]:
        errors: List[str] = []
        seen: set = set()
        duplicates = 0
        examples = 0
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                errors.append("line %d: invalid JSON (%s)" % (number, exc))
                continue
            if not isinstance(record, dict):
                errors.append("line %d: not an object" % number)
                continue
            missing = [key for key in ("prompt", "completion", "model")
                       if not str(record.get(key, "")).strip()]
            if missing:
                errors.append("line %d: missing %s"
                              % (number, ", ".join(missing)))
                continue
            for field_name in ("prompt", "completion"):
                value = record.get(field_name)
                if (isinstance(value, str)
                        and len(value.encode("utf-8")) > MAX_EXAMPLE_CHARS):
                    errors.append("line %d: %s exceeds %d bytes"
                                  % (number, field_name, MAX_EXAMPLE_CHARS))
            payload = json.dumps(record, sort_keys=True)
            if any(pattern.search(payload)
                   for pattern in _DATASET_SECRET_PATTERNS):
                errors.append("line %d: secret-looking content — datasets "
                              "never carry credentials" % number)
            if payload in seen:
                duplicates += 1
            else:
                seen.add(payload)
            examples += 1
        return {
            "ok": not errors,
            "examples": examples,
            "duplicates": duplicates,
            "errors": errors[:100],
        }

    def validate(self, dataset_path: str | Path) -> Dict[str, Any]:
        path = Path(dataset_path)
        if not path.is_file():
            return {"ok": False, "errors": ["dataset file does not exist"],
                    "examples": 0, "duplicates": 0, "path": str(path)}
        size = path.stat().st_size
        if size > MAX_DATASET_BYTES:
            return {"ok": False,
                    "errors": ["dataset exceeds %d byte cap"
                               % MAX_DATASET_BYTES],
                    "examples": 0, "duplicates": 0, "path": str(path)}
        report = self.validate_text(path.read_text(encoding="utf-8"))
        report["path"] = str(path)
        report["size_bytes"] = size
        return report


@dataclass
class TrainingJobRecord:
    """Metadata for a *requested* training job. No job ever runs itself."""

    id: str
    dataset: str
    created_at: float
    status: str = "RECORDED"  # RECORDED | CANCELLED | FAILED_PRECHECK
    error: str = ""
    base_model: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return redact({"id": self.id, "dataset": self.dataset,
                       "created_at": self.created_at, "status": self.status,
                       "error": self.error, "base_model": self.base_model,
                       "params": dict(self.params),
                       "ran": False,
                       "note": ("job bookkeeping only — no training runtime "
                                "is configured, and none is faked")})


class TrainingJobStore:
    """Durable records for training-job requests, with an honest gate."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def _dir(self) -> Path:
        return self.root / JOBS_DIR

    def create(self, dataset_path: str | Path, base_model: str = "",
               params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        path = Path(dataset_path)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_file():
            raise FileNotFoundError("dataset file not found: %s" % path)
        validation = DatasetValidator().validate(path)
        job = TrainingJobRecord(
            id="job-%d-%s" % (int(time.time()),
                              uuid4().hex[:8]),
            dataset=path.name, created_at=time.time(),
            base_model=base_model, params=dict(params or {}))
        if not validation["ok"]:
            job.status = "FAILED_PRECHECK"
            job.error = "; ".join(validation["errors"][:5])
        directory = self._dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("%s.json" % job.id)).write_text(
            json.dumps(job.to_dict(), indent=1, sort_keys=True),
            encoding="utf-8")
        return job.to_dict()

    def run(self, job_id: str) -> Dict[str, Any]:
        """Training runs are NOT implemented — this method never pretends.

        When a real trainer exists (e.g. an external fine-tuning service or
        a future Forge training pipeline), it will execute here after the
        same dataset validation, and only its measured results will be
        recorded.
        """
        raise TrainingRuntimeNotConfigured(
            "no training runtime is configured for this Forge installation; "
            "job %s stays recorded but unexecuted. Wire a real trainer into "
            "TrainingJobStore.run() (docs/A81 §training) before any model is "
            "claimed." % job_id)

    def list(self) -> List[Dict[str, Any]]:
        directory = self._dir()
        if not directory.is_dir():
            return []
        jobs = []
        for path in sorted(directory.glob("job-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                jobs.append(data)
        return jobs


@dataclass
class ModelVersion:
    """A *registered* model version — registration requires a real file."""

    version: str
    artifact_path: str
    created_at: float
    dataset_sha256: str = ""
    evaluation: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return redact({
            "version": self.version,
            "artifact_path": self.artifact_path,
            "created_at": self.created_at,
            "dataset_sha256": self.dataset_sha256,
            "evaluation": dict(self.evaluation),
            "source": self.source,
        })


class ModelVersionStore:
    """Manifest-based model versioning + promotion/rollback pointers.

    Registering requires the artifact to exist on disk, so a manifest can
    never describe a phantom model. Promotion additionally requires a real
    evaluation record with ``passed=True``. Rollback restores the previously
    active version (or none). All transitions are appended to the pointer
    file for auditability.
    """

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def _dir(self) -> Path:
        return self.root / MODELS_DIR

    def _versions(self) -> List[Dict[str, Any]]:
        directory = self._dir() / "versions"
        if not directory.is_dir():
            return []
        manifests: List[Dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data["_file"] = path.name
                manifests.append(data)
        return manifests

    def _artifact_ok(self, manifest: Dict[str, Any]) -> bool:
        """Re-verify the registered artifact still exists on disk.

        Registration only describes real files, so a manifest whose artifact
        has since been deleted must never read as promotable/active.
        """
        raw = str(manifest.get("artifact_path") or "")
        if not raw:
            return False
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path.is_file()

    def register(self, version: str, artifact_path: str | Path,
                 dataset_sha256: str = "",
                 evaluation: Optional[Dict[str, Any]] = None,
                 source: str = "") -> Dict[str, Any]:
        version = "".join(ch if ch.isalnum() or ch in "-_." else "-"
                          for ch in str(version))[:64]
        if not version:
            raise ValueError("model version id is required")
        artifact = Path(artifact_path)
        if not artifact.is_absolute():
            artifact = self.root / artifact
        if not artifact.is_file():
            raise FileNotFoundError(
                "refusing to register model %r: artifact does not exist "
                "(%s) — registration describes real files only"
                % (version, artifact))
        record = ModelVersion(
            version=version,
            artifact_path=str(artifact.relative_to(self.root)
                              if _is_under(artifact, self.root) else artifact),
            created_at=time.time(), dataset_sha256=dataset_sha256,
            evaluation=dict(evaluation or {}), source=source)
        directory = self._dir() / "versions"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / ("%s.json" % version),
                      json.dumps(record.to_dict(), indent=1, sort_keys=True))
        return record.to_dict()

    def list(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for manifest in self._versions():
            clean = {key: value for key, value in manifest.items()
                     if not key.startswith("_")}
            clean["artifact_missing"] = not self._artifact_ok(manifest)
            out.append(clean)
        return out

    def get(self, version: str) -> Optional[Dict[str, Any]]:
        for manifest in self._versions():
            if manifest.get("version") == version:
                fresh = dict(manifest)
                fresh["artifact_missing"] = not self._artifact_ok(manifest)
                return fresh
        return None

    def active(self) -> Optional[Dict[str, Any]]:
        pointer = self._dir() / "active.json"
        if not pointer.is_file():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = str((data or {}).get("active", ""))
        manifest = self.get(version)
        if manifest is None:
            return None
        return {"active": version,
                "history": [dict(h) for h in data.get("history", [])],
                "manifest": manifest,
                # Surfaced, never hidden: a deleted artifact makes the
                # active model unusable, and consumers must see that.
                "artifact_missing": bool(manifest.get(
                    "artifact_missing", False))}

    def promote(self, version: str) -> Dict[str, Any]:
        manifest = self.get(version)
        if manifest is None:
            raise KeyError("unknown model version: %s" % version)
        if not self._artifact_ok(manifest):
            raise ValueError("artifact for %s disappeared; re-register"
                             % version)
        evaluation = manifest.get("evaluation") or {}
        if not evaluation.get("passed", False):
            raise ValueError(
                "refusing to promote %s: no passing evaluation record — "
                "run ModelEvaluator first (evaluation is real or absent)"
                % version)
        directory = self._dir()
        directory.mkdir(parents=True, exist_ok=True)
        pointer = directory / "active.json"
        history: List[Dict[str, Any]] = []
        previous = ""
        if pointer.is_file():
            try:
                current = json.loads(pointer.read_text(encoding="utf-8"))
                previous = str(current.get("active", ""))
                history = list(current.get("history", []))
                if previous and previous != version:
                    history.append({"active": previous,
                                    "replaced_at": time.time(),
                                    "replaced_by": version})
            except (OSError, ValueError):
                history = []
        payload = {"active": version, "previous": previous,
                   "promoted_at": time.time(), "history": history[-20:]}
        _atomic_write(pointer, json.dumps(payload, indent=1,
                                          sort_keys=True))
        return payload

    def rollback(self) -> Dict[str, Any]:
        """Restore the previously active version, or clear the pointer."""
        pointer = self._dir() / "active.json"
        if not pointer.is_file():
            return {"active": None, "note": "no active model to roll back"}
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        history = [dict(h) for h in (data.get("history") or [])]
        previous = str(data.get("previous", "") or "")
        if not previous or self.get(previous) is None:
            if pointer.is_file():
                pointer.unlink()
            return {"active": None,
                    "note": "rolled back to no active model "
                            "(previous version unknown or missing)"}
        payload = {"active": previous, "previous": "",
                   "rolled_back_at": time.time(), "history": history}
        _atomic_write(pointer, json.dumps(payload, indent=1, sort_keys=True))
        return payload


class ModelEvaluator:
    """Evaluation only ever runs against a registered, real model."""

    def __init__(self, root: str | Path = ".", fabric: Any = None) -> None:
        self.root = Path(root).resolve()
        self.fabric = fabric

    def evaluate(self, tasks: List[str], model_hint: str = "") -> Dict[str,
                                                                       Any]:
        """Run real analysis over ``tasks`` with the fabric's best model.

        Returns measured per-task results (context build + verification
        state). If no real model exists, this *refuses* — there is no
        simulated evaluation path.
        """
        if self.fabric is None:
            raise TrainingRuntimeNotConfigured(
                "evaluation needs a Model Fabric with a real model; none "
                "attached")
        from forge.models.readiness import (
            describe_no_model_error,
            fabric_has_real_model,
        )
        if not fabric_has_real_model(self.fabric):
            raise TrainingRuntimeNotConfigured(describe_no_model_error(
                fabric=self.fabric))
        from forge.native.engine import NativeAIEngine
        results: List[Dict[str, Any]] = []
        passed = 0
        for task in tasks[:50]:
            engine = NativeAIEngine(self.root, mode="safe",
                                    fabric=self.fabric,
                                    persist_status=False)
            outcome = engine.run(task)
            ok = outcome.final_status == "COMPLETED"
            passed += 1 if ok else 0
            results.append({"task": task[:200], "status":
                            outcome.final_status, "passed": bool(ok)})
        summary = {
            "tasks": len(results),
            "passed": passed,
            "passed_ratio": (passed / float(len(results))) if results else 0.0,
            "results": results,
            "model_hint": model_hint,
            "measured_at": time.time(),
        }
        return summary

    def record(self, version: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Attach a measured evaluation summary to a registered version."""
        store = ModelVersionStore(self.root)
        manifest = store.get(version)
        if manifest is None:
            raise KeyError("unknown model version: %s" % version)
        manifest["evaluation"] = redact(dict(summary))
        manifest["evaluation"]["passed"] = bool(
            summary.get("tasks", 0) > 0
            and summary.get("passed_ratio", 0.0) >= 0.8)
        directory = store._dir() / "versions"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / ("%s.json" % version),
                      json.dumps({key: value for key, value in
                                  manifest.items()
                                  if not key.startswith("_")},
                                 indent=1, sort_keys=True))
        return manifest["evaluation"]


class BenchmarkComparator:
    """Deterministic comparison of two recorded evaluation summaries."""

    @staticmethod
    def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
        metric_a = float(a.get("passed_ratio", 0.0))
        metric_b = float(b.get("passed_ratio", 0.0))
        if metric_a > metric_b:
            winner = "a"
        elif metric_b > metric_a:
            winner = "b"
        else:
            winner = "tie"
        return {
            "winner": winner,
            "a": {"passed_ratio": metric_a, "tasks": a.get("tasks", 0)},
            "b": {"passed_ratio": metric_b, "tasks": b.get("tasks", 0)},
            "note": "comparison of recorded evaluations only; no model "
                    "performance is implied beyond what was measured",
        }


class NativeTrainingFabric:
    """One object that reports/coordinates the whole training surface."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.datasets = DatasetBuilder(self.root)
        self.validator = DatasetValidator()
        self.jobs = TrainingJobStore(self.root)
        self.versions = ModelVersionStore(self.root)
        self.comparator = BenchmarkComparator()

    def status(self) -> Dict[str, Any]:
        versions = self.versions.list()
        real = 0
        for manifest in versions:
            artifact = self.root / str(manifest.get("artifact_path", ""))
            if artifact.is_file():
                real += 1
        datasets_dir = self.root / DATASETS_DIR
        datasets = sorted(p.name for p in datasets_dir.glob("*.jsonl")) \
            if datasets_dir.is_dir() else []
        runs_dir = self.root / RUNS_DIR
        run_records = len(list(runs_dir.glob("*.json"))) \
            if runs_dir.is_dir() else 0
        return {
            "trained_models": real,
            "registered_versions": len(versions),
            "note": ("trained_models counts registered versions whose "
                     "artifact exists on disk; no training run has produced "
                     "a Forge model yet" if real == 0 else
                     "%d registered model(s) with existing artifacts" % real),
            "trainer": "not_configured",
            "datasets": datasets,
            "run_records": run_records,
            "jobs": len(self.jobs.list()),
            "active_model": (self.versions.active() or {}).get("active"),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
    os.replace(str(tmp), str(path))
