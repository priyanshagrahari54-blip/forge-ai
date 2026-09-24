# FORGE AI — AGENT SYSTEM
## 1. Fleet
40 specialization families × 26 variants = 1,040 logical specialists. They are persistent identities/configurations, not simultaneously running heavyweight models.

## 2. Agent contract
Every agent has: id, family, variant, purpose, canonical capabilities, model preferences, fallback chain, tools, memory scope, security profile, evaluation profile and telemetry identity.

## 3. Capability normalization
Specialization labels map to canonical capabilities. Example: frontend→coding/browser/tool_use; security→security/reasoning/coding; researcher→research/reasoning/long_context.

## 4. Lifecycle
REGISTERED → VALIDATED → AVAILABLE → SELECTED → AUTHORIZED → EXECUTING → RESULT → VERIFIED → EVALUATED. Failures are explicit and recoverable.

## 5. Selection
Intent → required capabilities → specialist → eligible verified models → policy → context/modality → preference → reliability/latency/cost → execution.

## 6. Multi-agent orchestration
Use the minimum sufficient set. Parallelize independent work; serialize dependent work. Every subtask has parent task, run, agent and attempt identity.

## 7. Agent memory
Scopes: user, project, session, task, agent. Cross-scope retrieval requires explicit policy.

## 8. Tool access
Agents never grant themselves permissions. Tool calls pass through authorization and risk policy.

## 9. Evaluation
Measure correctness, task success, verification pass rate, latency, cost, tool success, retry rate and user corrections.

## 10. Evolution
Observation → hypothesis → candidate → benchmark → security review → approval → deployment → monitoring → rollback. No uncontrolled self-modification.
