# FORGE AI — AGENT SYSTEM v2

## 1. Fleet
40 specialization families × 26 variants = 1,040 logical specialists. They are identities/configurations, not 1,040 simultaneously running heavyweight models.

## 2. Agent contract
Every agent has: id, family, variant, purpose, canonical capabilities, model pool/preferences, fallback chain, tools, memory scope, security profile, evaluation profile, provenance and telemetry identity.

## 3. Agent creation policy
Forge may create a new specialist when a real capability gap exists. It must first search the existing capability ecosystem and prefer REUSE/ADAPT/COMPOSE/EXTEND. New agents must have a measurable purpose and evaluation contract.

## 4. Capability normalization
Specialization labels map to canonical capabilities. A role name is never treated as proof that a capability is actually available.

## 5. Lifecycle
DISCOVERED → REGISTERED → VALIDATED → AVAILABLE → SELECTED → AUTHORIZED → EXECUTING → RESULT → VERIFIED → EVALUATED. Failures are explicit and recoverable.

## 6. Selection
Intent → master requirement contract → required capabilities → specialist → verified eligible models/tools → security policy → context/modality → reliability/latency/cost → execution.

## 7. Multi-agent orchestration
Use the minimum sufficient set. Parallelize independent subtasks; serialize dependent work. Each subtask has parent task, run, agent and attempt identity. Multiple models may serve different stages of one task.

## 8. Tool access
Agents never grant themselves permissions. All tool calls pass through authorization, risk and capability-health policy.

## 9. Quality
An agent cannot mark a result complete merely because it produced output. Required tests, evidence, contract acceptance and final quality verification apply to the parent task.

## 10. Evaluation
Measure correctness, task success, contract satisfaction, verification pass rate, quality/benchmark score, latency, cost, tool success, retry rate and user corrections.

## 11. Controlled evolution
Observation → hypothesis → candidate → sandbox → benchmark → regression → security review → approval → deployment → monitoring → rollback. No uncontrolled self-modification or silent security-policy changes.
