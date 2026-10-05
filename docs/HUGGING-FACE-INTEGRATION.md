# Forge × Hugging Face Integration

## Why Hugging Face is a first-class ecosystem adapter
Hugging Face Hub provides models, datasets and Spaces, while Inference Providers provides a unified route to many hosted models. Forge therefore treats HF as an ecosystem/catalog + inference-provider adapter, not as a single model.

## Current integration
- Provider name: `huggingface`
- Router: `https://router.huggingface.co/v1`
- Authentication: `HF_TOKEN` or `HUGGINGFACEHUB_API_TOKEN`
- Model: `HF_MODEL` — deliberately required; Forge does not invent a model.
- Protocol: OpenAI-compatible chat completions.
- Provider selection: HF router can use `auto`; future Forge policy can add explicit provider preferences.
- Registration: only when token + model are configured.
- Verification: normal Forge model verification is still required before production routing.

## Hugging Face ecosystem roadmap
### Models
Discover model metadata, task/pipeline type, modality, context where available, license/model card, provider availability and evaluation evidence. Register only after policy checks.

### Datasets
Add a dataset adapter for discovery, metadata, streaming, provenance, license/access checks and controlled ingestion into Forge RAG/training workflows. Never treat a public dataset as automatically license-compatible for every use.

### Spaces
Use Spaces as discoverable application/demo capabilities. Source and access policy must be inspected before execution or reuse.

### Training/fine-tuning
Use Hugging Face Transformers/TRL/PEFT/Datasets ecosystem through adapters. Forge owns orchestration, dataset provenance, evaluation and acceptance; the actual training runtime may be external because the G560 is a thin client.

## Zero-rupee policy
HF has a free tier for some inference usage, but availability/credits and provider pricing can change. Forge must not claim an operation is free merely because HF is configured. Cost/credit status is a routing input and a production task must disclose when paid infrastructure is required.

## Security
Tokens remain in the credential system/environment and are never written into model metadata, prompts, logs or generated artifacts. External model/data content is untrusted input. License, provenance and security checks happen before adoption.


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
