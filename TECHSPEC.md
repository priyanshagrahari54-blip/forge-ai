# FORGE AI — TECHNICAL SPECIFICATION
## API
Versioned endpoints, validated schemas, authenticated requests and stable error objects.
Success: {success:true,request_id,data}
Error: {success:false,request_id,error:{code,message}}

## ModelRequest
prompt, capability, required_capabilities, context, task_id, caller, model, preferred_models, fallback_models, complexity, modality, timeout, output limits, security policy, context pack, evidence and metadata.

## ModelResponse
success, text, model, provider, request_id, latency, usage, finish_reason, error and metadata.

## Provider contract
list_models(), health(), infer(). Adapters normalize authentication, request/response, timeout, rate limit, usage and errors.

## Agent contract
execute(request) → structured AgentResponse with success, output/error, agent identity, task/run/attempt IDs, artifacts and verification metadata.

## Tool contract
tool_id, capabilities, input/output schema, permissions, risk, timeout, network policy, retry policy and audit behavior.

## Memory record
memory_id, scope, type, content, source, confidence, created_at, updated_at, expires_at, provenance, retention policy.

## Evidence record
evidence_id, source, claim, supporting text/data, publication/retrieval time, source type, confidence and citation.

## Event
event_id, version, event_type, task_id, run_id, actor, timestamp, payload.

## Lease
task_id, worker_id, lease_id, attempt_id, leased_at, expires_at, heartbeat_at.

## Routing
Candidate discovery → canonical capabilities → verification → policy → context/modality → preference → health/reliability/latency/cost → inference.

## Retry
Retry only transient failures. Do not blindly retry invalid credentials, authorization denial or malformed requests.

## Observability
request_id/task_id/run_id/attempt_id/provider/model/agent/tool/latency/status/error/retry count.
