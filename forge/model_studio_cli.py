"""Command-line entry point for Forge Model Studio."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from forge.models.builtin_profiles import FORGE_PROFILES
from forge.models.model_studio import ModelStudio


def _load_jsonl(path: Path) -> Iterable[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise SystemExit("invalid JSON on line %d: %s" % (number, exc))
            if not isinstance(value, dict):
                raise SystemExit("line %d is not a JSON object" % number)
            yield value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="forge-model", description="Forge Model Studio")
    sub = parser.add_subparsers(dest="command")

    profile = sub.add_parser("profile", help="show a built-in specialization profile")
    profile.add_argument("name", choices=sorted(FORGE_PROFILES))
    profile.add_argument("--json", action="store_true")

    validate = sub.add_parser("validate", help="validate and fingerprint a JSONL SFT dataset")
    validate.add_argument("dataset")
    validate.add_argument("--out", default="")

    plan = sub.add_parser("plan", help="generate a bounded LoRA/QLoRA training plan")
    plan.add_argument("base_model")
    plan.add_argument("profile", choices=sorted(FORGE_PROFILES))
    plan.add_argument("--dataset-size", type=int, default=0)
    plan.add_argument("--method", choices=("lora", "qlora", "full", "prompt"), default="")
    plan.add_argument("--quantization", choices=("none", "4bit", "8bit"), default="")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    studio = ModelStudio(".")
    if args.command == "profile":
        data = FORGE_PROFILES[args.name]
        print(json.dumps({
            "name": data.name,
            "description": data.description,
            "domains": list(data.domains),
            "capabilities": list(data.capabilities),
            "preferred_tasks": list(data.preferred_tasks),
            "max_context_tokens": data.max_context_tokens,
            "max_output_tokens": data.max_output_tokens,
            "quality_floor": data.quality_floor,
            "system_instruction": data.system_instruction,
        }, indent=2 if args.json else None, sort_keys=True))
        return 0

    if args.command == "validate":
        records, report = studio.validate_dataset(_load_jsonl(Path(args.dataset)))
        print(json.dumps({
            "report": report.to_dict(),
            "dataset_fingerprint": studio.fingerprint_dataset(records),
        }, indent=2, sort_keys=True))
        if args.out:
            output = Path(args.out)
            with output.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return 0 if report.ok else 2

    if args.command == "plan":
        profile = FORGE_PROFILES[args.profile]
        training_plan = studio.make_training_plan(
            args.base_model,
            profile,
            dataset_size=max(0, args.dataset_size),
            method=args.method or None,
            quantization=args.quantization or None,
        )
        print(json.dumps(training_plan.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
