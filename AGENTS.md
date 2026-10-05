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


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
