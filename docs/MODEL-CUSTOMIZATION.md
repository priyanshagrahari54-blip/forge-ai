# Forge Model Customization

Forge keeps model customization separate from model execution, while connecting them through the same verified inference fabric.

## Pipeline

1. **Collect** high-quality instruction, coding, debugging, review, security, web, and game/3D examples.
2. **Sanitize** malformed, oversized, duplicate, secret-bearing, or unsafe records.
3. **Split** deterministically into training and validation sets and fingerprint the exact dataset.
4. **Specialize** with LoRA/QLoRA or another explicitly selected method.
5. **Train** only through a registered training backend. A placeholder is never marked `trained`.
6. **Benchmark** representative Forge engineering tasks.
7. **Gate promotion** on average quality, category floors, critical failures, and regression tolerance.
8. **Verify** the promoted artifact before serving it through the Native Model Runtime.
9. **Route** the promoted model through capability, policy, resource, residency, and fence checks.

## Specialization targets

- `forge-coding`: implementation, architecture, repository editing, tests.
- `forge-debug`: failure diagnosis, minimal repair, regression prevention.
- `forge-security`: secure coding, threat analysis, policy-safe remediation.
- `forge-web`: frontend/backend/full-stack project generation.
- `forge-game3d`: game architecture, assets, gameplay systems, tools, tests.

These are **training objectives**, not claims that the repository already contains trained weights.

## Artifact rule

Model weights and adapters belong in operator-controlled runtime storage under `.forge/models/` and are ignored by source control. Forge records provenance and verification metadata, but never commits model artifacts as source.

## Runtime rule

A customized model is production-usable only when:

`registered → reachable → verified → resource-admissible → policy-admissible`

The deterministic reference backend remains a test mechanism and is never promoted as a real coding model.
