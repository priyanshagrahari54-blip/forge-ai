# FORGE AI — TECHNICAL SPEC
## API
Authenticated/versioned endpoints, validated schemas and stable errors.
Success: {success:true,request_id,data}; Error: {success:false,request_id,error:{code,message}}.

## ModelRequest
prompt, capability, required_capabilities, context, task_id, caller, model, preferred_models, fallback_models, complexity, modality, timeout, output limits, security policy, context pack, evidence and metadata.

## ModelResponse
success, text, model, provider, request_id, latency, usage, finish_reason, error, metadata.

## Provider
list_models(), health(), infer(); adapters normalize auth, request/response, errors, timeout, rate limits and telemetry.

## Agent
execute(request) → structured response containing agent/task/run/attempt identity, output/error, artifacts and verification metadata.

## Tool
tool_id, capabilities, input/output schema, permissions, risk, network policy, timeout, retry and audit behavior.

## Memory
id, scope, type, content, source, confidence, timestamps, expiration, provenance and retention policy.

## Evidence
id, source, claim, supporting evidence, publication/retrieval time, source type, confidence and citation.

## Event
event_id, version, event_type, task_id, run_id, actor, timestamp, payload.

## Lease
task_id, worker_id, lease_id, attempt_id, leased_at, expires_at, heartbeat_at.

## Routing
candidate → capability → verification → policy → context/modality → preference → health/reliability/latency/cost.

## Retry
Only transient failures are retried. Credential/authorization/schema failures are not blindly retried.

## Observability
request_id, task_id, run_id, attempt_id, provider, model, agent, tool, latency, status, error code, retry count.
