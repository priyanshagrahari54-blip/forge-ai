# FORGE AI — CONTINUOUS AUTONOMY & UNBOUNDED WORK

## Objective
The user should be able to submit work continuously and let Forge execute eligible tasks automatically without an arbitrary total-task limit.

## Important meaning of unlimited
**Unlimited work queue ≠ unlimited free compute.** Forge may keep an unbounded logical backlog, continue completed tasks into the next queued task, retry/replan recoverable failures, and rotate between verified providers. It must never fabricate unlimited GPU/API capacity.

## Continuous loop
```text
USER GOAL
  ↓
CONTRACT
  ↓
PLAN / TASK GRAPH
  ↓
QUEUE
  ↓
SELECT VERIFIED CAPABILITY
  ↓
EXECUTE
  ↓
TEST / SECURITY / QUALITY
  ├─ PASS → DELIVER → NEXT ELIGIBLE TASK
  └─ FAIL → DIAGNOSE → REPAIR/REPLAN → RETEST
                         ↓
                 provider exhausted?
                   ├─ yes → rotate
                   └─ no  → continue
                         ↓
                 all providers exhausted
                         ↓
                       QUEUE
```

## No artificial caps
A total project/task-count cap must not be used as a definition of autonomy. Runtime concurrency, per-action safety limits, provider quotas, memory limits, timeouts and compute budgets remain independent resource controls.

## Provider exhaustion
1. Detect actual exhaustion/health failure.
2. Try another verified provider satisfying the contract.
3. If none exists, preserve the task as queued/paused rather than claiming success.
4. Resume when capacity returns or the user adds an eligible provider.

## Hugging Face
Hugging Face is a first-class ecosystem source for models, datasets and Spaces. Forge may use HF Inference Providers, Hub discovery and compatible Spaces as capability sources, subject to actual availability, authentication, licensing and quotas.

HF currently documents monthly free Inference Provider credits for Free users, but these are small and can change; additional inference is pay-as-you-go. Therefore Forge must not treat HF as an unlimited free inference backend.

HF ZeroGPU provides shared GPU access for Spaces, but free accounts have daily quotas. It is useful for opportunistic workloads, demos and burst execution, not as a promise of unlimited training/inference.

## Zero-rupee policy
When no verified free capacity exists, Forge does not silently incur a charge. It queues, pauses, degrades only with explicit permission, or asks for a resource/provider decision.

## Autonomy safety
Automatic continuation never bypasses: requirement contracts, authorization, risky-action approval, secrets policy, sandboxing, security gates, tests, benchmarks or final quality acceptance.

## Checkpoint/recovery
Long-running work should checkpoint at meaningful boundaries. A failed repair or provider switch must not destroy the last accepted state.

## Success definition
A continuous autonomous session may contain thousands or more logical subtasks over time. The system is successful when it keeps making verified progress while honestly respecting available resources and never pretending that unavailable capacity exists.
