"""Fine-tune specialist adapters from real Forge execution evidence.

Pipeline (every step real, nothing invented):

1. **evidence**  read one or more fleet-run artifacts
   (``scripts/run_fleet_real.py --out …``) and keep only records where a real
   model actually produced output;
2. **dataset**   validate, deduplicate and fingerprint them through the Model
   Studio's validator (secret-bearing rows are rejected, never trained on);
3. **preflight**  check the training stack, the registered trainer, the dataset
   size and the free disk, and print exactly what is missing;
4. **train**     hand the validated dataset to the registered backend;
5. **report**    write a job report (state, blockers, artifact, provenance,
   dataset fingerprint) that the readiness report can quote.

The script never reports ``TRAINED`` without an artifact the backend itself
marked trained, and it exits non-zero with ``BLOCKED`` when a fine-tune cannot
be run on this machine: a missing training stack or a machine that cannot hold
a training run is a real dependency, not something to paper over.

Usage::

    .venv/bin/python scripts/finetune_specialists.py \\
        --dataset docs/evidence/fleet-real-run-2026-09-19.json \\
        --specialization coding \\
        --out .forge/models/coding-finetune-report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from forge.models.model_studio import ModelStudio               # noqa: E402
from forge.training.dataset import build_dataset                # noqa: E402
from forge.training.job import (                                # noqa: E402
    FineTuneJob,
    JobState,
    register_default_trainers,
)

BLOCKED_EXIT = 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", action="append", default=[],
                        help="fleet-run evidence file (repeatable)")
    parser.add_argument("--specialization", default="coding",
                        help="which specialist adapter to train")
    parser.add_argument("--base-model", default="SmolLM2-135M-Instruct")
    parser.add_argument("--trainer", default="",
                        help="registered trainer name (default: best available)")
    parser.add_argument("--method", default="lora",
                        choices=["lora", "qlora", "full", "prompt"])
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--steps", type=int, default=30,
                        help="optimizer steps for the local GGUF LoRA trainer")
    parser.add_argument("--minimum-records", type=int, default=8)
    parser.add_argument("--allow-network", action="store_true",
                        help="let the training backend download the base model")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    sources = [Path(item) for item in args.dataset] or [
        Path("docs/evidence/fleet-real-run-2026-09-19.json")]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        print("BLOCKED: no execution evidence to learn from.")
        print(f"  reason     : missing {', '.join(missing)}")
        print("  implemented: the whole pipeline (dataset builder, validator, "
              "training job, studio integration)")
        print("  requirement: run scripts/run_fleet_real.py against a "
              "runtime-verified model to produce evidence first")
        return BLOCKED_EXIT

    studio = ModelStudio(root=str(REPO))
    declared = register_default_trainers(studio, allow_network=args.allow_network,
                                        base_model=args.base_model,
                                        steps=args.steps)
    bundle = build_dataset(sources, studio=studio,
                           output=REPO / ".forge" / "models"
                           / f"{args.specialization}-finetune.jsonl")
    print(f"dataset        : {bundle.path}")
    print(f"examples       : {len(bundle.records)} "
          f"(of {len(bundle.records) + bundle.report['duplicates_removed'] + bundle.report['rejected']} "
          f"read; {bundle.report['duplicates_removed']} duplicate, "
          f"{bundle.report['rejected']} rejected, "
          f"{bundle.report['secret_bearing']} secret-bearing)")
    print(f"roles          : {bundle.by_role}")
    print(f"fingerprint    : {bundle.fingerprint[:32]}…")
    print(f"trainers       : {studio.trainers() or 'none registered'}"
          f"{' (auto-registered: ' + ', '.join(declared) + ')' if declared else ''}")

    job = FineTuneJob(
        specialization=args.specialization,
        dataset_path=bundle.path,
        base_model=args.base_model,
        records=bundle.records,
        studio=studio,
        #: Prefer the backend that needs nothing from the network.
        trainer=args.trainer or next(
            (name for name in ("gguf-lora", "huggingface-peft")
             if name in studio.trainers()),
            (studio.trainers() or [""])[0]),
        epochs=args.epochs,
        method=args.method,
    )
    state = job.preflight(minimum_records=args.minimum_records)
    print(f"preflight      : {state.value}")
    for blocker in job.blockers:
        print(f"  blocked      : {blocker}")
    for requirement in job.requirements:
        print(f"  requirement  : {requirement}")

    if state is JobState.READY:
        print(f"training       : {job.trainer} × {args.epochs} epoch(s) on "
              f"{len(job.records)} examples …")
        artifact = job.run(minimum_records=args.minimum_records)
        if artifact is not None:
            print(f"TRAINED        : {artifact.path}")
            print(f"  provenance   : base={artifact.provenance.base_model} "
                  f"method={artifact.provenance.method} "
                  f"trainer={artifact.provenance.trainer}")
        else:
            print(f"FAILED         : {job.error}")

    report = job.to_dict()
    report["dataset_fingerprint"] = bundle.fingerprint
    report["dataset_by_role"] = bundle.by_role
    report["dataset_report"] = bundle.report
    report["sources"] = bundle.sources
    report["registered_trainers"] = studio.trainers()
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {target}")

    return 0 if job.state is JobState.TRAINED else BLOCKED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
