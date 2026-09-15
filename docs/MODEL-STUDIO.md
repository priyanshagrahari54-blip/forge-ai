# Forge Model Studio

Forge now treats model improvement as a first-class engineering loop alongside software-engineering execution.

## What Forge customizes

Model Studio manages four distinct layers:

1. **Data** — JSONL instruction/SFT records are normalized, bounded, deduplicated, secret-scanned, fingerprinted, and deterministically split.
2. **Specialization** — built-in targets define the behavior required for coding, debugging, security, web, and game/3D engineering.
3. **Training** — LoRA/QLoRA/full/prompt tuning plans are generated with conservative resource defaults. Actual weight training is performed only by an explicitly registered `TrainingBackend`.
4. **Evaluation and promotion** — candidates are benchmarked before promotion. Average quality, per-category floors, and regression thresholds can block a candidate.

This distinction is intentional: a plan or adapter metadata is **not** considered trained weights. `ModelProvenance.trained` must be true before `ModelStudio.train()` accepts an artifact.

## CLI

```text
forge-model profile forge-coding
forge-model profile forge-debug --json
forge-model validate dataset.jsonl --out clean.jsonl
forge-model plan <base-model> forge-coding --dataset-size 50000
```

The base install stays compatible with Forge's Python 3.8 portability target because Model Studio itself uses only the standard library.

## Side-by-side model evolution

The intended loop is:

```text
Repository/task traces
        |
        v
 Dataset validation + fingerprint
        |
        +--> specialization profile
        |
        v
 LoRA/QLoRA training backend
        |
        v
 Candidate artifact + provenance
        |
        v
 Regression / capability benchmarks
        |
   +----+----+
   |         |
  FAIL      PASS
   |         |
 reject    promote
             |
             v
 ModelCatalog -> Verification -> Residency -> Routing -> InferenceFabric
```

Forge therefore upgrades the model and the engineering system in the same control loop. A model cannot silently become the production model: it must have provenance, verification, benchmark evidence, and an explicit promotion result.
