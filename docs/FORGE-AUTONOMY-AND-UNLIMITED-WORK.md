# FORGE AI — AUTONOMOUS / CONTINUOUS WORK POLICY

## What “unlimited work” means
Forge must not impose an artificial lifetime task-count limit. The user can submit task after task, projects can remain queued, and completed work can be followed by new work automatically.

This is **not** a claim of unlimited compute, unlimited API credits, unlimited GPU, unlimited bandwidth or unlimited third-party service usage.

## Continuous execution loop
USER GOAL
→ REQUIREMENT CONTRACT
→ TASK GRAPH
→ QUEUE
→ CAPABILITY/MODEL ROUTING
→ EXECUTE
→ VERIFY
→ REPAIR
→ COMPLETE
→ NEXT TASK
→ CONTINUE

## Provider exhaustion
When a provider reaches a real quota/rate/credit limit:
1. record the exact signal;
2. stop wasting calls against that provider;
3. rotate to another eligible verified provider/model;
4. use free/local/available resources first when quality remains acceptable;
5. discover another capability/provider when policy permits;
6. resume queued work;
7. never fabricate success.

## Hugging Face
Hugging Face is an ecosystem source, not an unlimited compute guarantee. Current HF documentation describes monthly credits for Inference Providers and pay-as-you-go beyond included credits; free users therefore cannot honestly be promised unlimited hosted inference. citeturn0search0

Forge should use HF for:
- model discovery and metadata;
- datasets and dataset metadata;
- model cards/provenance;
- Inference Providers when actually available;
- Spaces/Jobs/Endpoints where the user has legitimate access;
- Transformers/PEFT/TRL ecosystem integration;
- downloading/running permitted open models only when local or external compute is actually available.

## Zero-rupee strategy
For the user's target, Forge should maximize useful work rather than promise impossible infinite hosted inference:
- local/free verified resources first;
- free hosted quotas where available;
- rotate among independently available providers;
- cache deterministic/reusable results;
- batch work;
- avoid redundant model calls;
- use small models for routine subtasks;
- reserve expensive models for quality-critical subtasks;
- pause safely when all eligible compute is exhausted;
- resume automatically when a provider becomes available again.

## Safety limits
Continuous work still has bounded per-attempt runtime, retries, permissions, concurrency and resource use. These prevent infinite retry loops, runaway costs and destructive autonomous behavior.

## Acceptance rule
A task is successful only after the normal requirement and quality gates pass. A provider timeout, quota exhaustion or partial artifact is not success.
