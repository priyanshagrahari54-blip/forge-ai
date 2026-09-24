# FORGE AI — AGENT MASTER SPEC
## Fleet
40 families × 26 variants = 1,040 logical specialists. Logical identity is independent of underlying model execution.

## Agent schema
id, family, variant, purpose, canonical capabilities, preferred_models, fallback_models, tools, memory_scope, security_profile, evaluation_profile, routing history and telemetry.

## Canonical capabilities
coding, reasoning, planning, debugging, testing, review, security, research, documentation, vision, image_generation, audio, speech_to_text, text_to_speech, browser, computer_use, tool_use, structured_output, long_context.

## Mapping
frontend→coding/browser/tool_use; backend→coding/reasoning/tool_use; database→coding/structured_output/reasoning; security→security/reasoning/coding; researcher→research/reasoning/long_context.

## Lifecycle
REGISTERED → VALIDATED → AVAILABLE → SELECTED → AUTHORIZED → EXECUTING → RESULT → VERIFIED → EVALUATED. Failures are explicit.

## Routing
task intent → required capabilities → specialist → eligible verified models → policy → context/modality → preference → reliability → latency → cost → execution.

## Multi-agent
Use minimum sufficient agents. Parallelize independent nodes; serialize dependencies. Every node has task_id/run_id/attempt_id/agent_id.

## Tools and security
Agents cannot grant themselves permissions. Every tool call passes authorization, risk checks and output validation.

## Evaluation
Correctness, task success, verification pass rate, latency, cost, tool success, retry rate, user corrections and security compliance.

## Evolution
Observation → hypothesis → candidate → sandbox → benchmark → regression → security review → approval → deployment → monitoring → rollback. No uncontrolled self-modification.
