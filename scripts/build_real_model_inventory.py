"""Build a checked-in inventory of 1000+ real Hugging Face model IDs.

Usage:
    python scripts/build_real_model_inventory.py
    python scripts/build_real_model_inventory.py --limit 1500

The script fetches IDs from Hugging Face; it never manufactures model names.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from forge.models.oss_registry import discover_huggingface_models, oss_catalog


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output", default="docs/FORGE-REAL-1000-MODELS.txt")
    args = parser.parse_args()

    discovered = discover_huggingface_models(limit=args.limit)
    models = oss_catalog(discovered=discovered)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "FORGE — REAL OPEN MODEL INVENTORY",
        "===================================",
        f"Concrete model IDs: {len(models)}",
        "Source: Hugging Face public model registry + Forge verified seeds",
        "Status note: catalogued IDs are NOT automatically configured or LIVE.",
        "",
    ]
    for index, model in enumerate(models, 1):
        tags = ", ".join(model.tags) if model.tags else "-"
        lines.append(f"{index:04d}. {model.model_id} | provider={model.provider} | source={model.source} | tags={tags}")

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(models)} real model IDs to {output}")


if __name__ == "__main__":
    main()
