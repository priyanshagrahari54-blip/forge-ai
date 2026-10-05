# FORGE AI — MODEL CATALOG & ROUTING v2

## 1. Principle
Forge is provider-neutral. A provider/model is a capability source, not the architecture itself. Multiple providers may be used per task and per stage.

## 2. Status vocabulary
CATALOGUED, REGISTERED, CONFIGURED, DISCOVERED, REACHABLE, VERIFIED, LIVE, BLOCKED, ERROR.

## 3. Required metadata
provider, model_id, capabilities, context length, modalities, reasoning/tool support where known, endpoint class, verification timestamp, latency, reliability, cost metadata, limitations, license/access class and failure history.

## 4. Truth rule
Discovery alone never makes a model LIVE. A model becomes VERIFIED only after an actual bounded probe and policy checks. Reachability is not quality proof.

## 5. Routing
Intent/contract → required capabilities → eligible models → security/access policy → modality/context → quality/reliability → latency/cost → selection → bounded execution → evidence/telemetry.

## 6. Multi-model tasks
A single task may use different models for planning, coding, research, vision, critique, verification, voice or final synthesis. Selection is stage-specific.

## 7. Provider fallback
Fallbacks are policy-driven. Forge may switch providers when the selected provider is unavailable or unsuitable, provided the replacement satisfies the task contract and quality/security constraints.

## 8. No imaginary capability
A model name, package, endpoint, environment variable or configuration entry is not evidence that the model is actually usable. UI and orchestration must expose the distinction.

## 9. Fine-tuning/model building
Fine-tuning and model-building workflows are capabilities routed to actual available infrastructure. Forge must report compute, data, licensing and provider limitations honestly.


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
