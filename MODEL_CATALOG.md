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
