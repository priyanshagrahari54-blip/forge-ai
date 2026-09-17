"""Build a checked-in inventory of 1000+ real Hugging Face model IDs.

Usage:
    python scripts/build_real_model_inventory.py
    python scripts/build_real_model_inventory.py --limit 1500
    python scripts/build_real_model_inventory.py --limit 1500 --json-output docs/FORGE-REAL-MODELS.json

The script fetches IDs from Hugging Face; it never manufactures model names.
It fails closed if the requested minimum cannot be satisfied.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from forge.models.oss_registry import discover_huggingface_models, oss_catalog

MINIMUM_REAL_MODELS = 1000
MAX_DISCOVERY = 5000


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Forge's real model inventory")
    parser.add_argument("--limit", type=int, default=MINIMUM_REAL_MODELS)
    parser.add_argument("--output", default="docs/FORGE-REAL-1000-MODELS.txt")
    parser.add_argument("--json-output", default=None)
    args = parser.parse_args()

    if args.limit < MINIMUM_REAL_MODELS or args.limit > MAX_DISCOVERY:
        raise SystemExit(
            f"--limit must be between {MINIMUM_REAL_MODELS} and {MAX_DISCOVERY}; "
            "Forge does not lower the real-model target silently."
        )

    discovered = discover_huggingface_models(limit=args.limit)
    models = oss_catalog(discovered=discovered)

    if len(models) < MINIMUM_REAL_MODELS:
        raise SystemExit(
            f"Discovery returned only {len(models)} unique real model IDs; "
            f"at least {MINIMUM_REAL_MODELS} are required. No incomplete inventory written."
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "FORGE — REAL OPEN MODEL INVENTORY",
        "===================================",
        f"Concrete model IDs: {len(models)}",
        "Minimum target: 1000",
        "Source: Hugging Face public model registry + Forge verified seeds",
        "Status note: catalogued IDs are NOT automatically configured or LIVE.",
        "Each identifier below is sourced from a public Hugging Face model ID.",
        "",
    ]
    for index, model in enumerate(models, 1):
        tags = ", ".join(model.tags) if model.tags else "-"
        lines.append(
            f"{index:04d}. {model.model_id} | provider={model.provider} | "
            f"source={model.source} | tags={tags}"
        )

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.json_output:
        json_output = Path(args.json_output)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "real_model_count": len(models),
            "minimum_target": MINIMUM_REAL_MODELS,
            "source": "huggingface-public-api",
            "availability_policy": "catalogued-is-not-live",
            "entries": [
                {
                    "model_id": model.model_id,
                    "provider": model.provider,
                    "source": model.source,
                    "status": model.status,
                    "license": model.license,
                    "tags": list(model.tags),
                }
                for model in models
            ],
        }
        json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(models)} real model IDs to {output}")


if __name__ == "__main__":
    main()
