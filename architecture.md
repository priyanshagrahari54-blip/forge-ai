# FORGE AI — MASTER ARCHITECTURE
## High-level
User → Chat/Voice/Cockpit → API/Auth → Control Plane → Supervisor/Task Engine → 1,040 Specialists → Model Fabric → Verified Models/Providers → Tools/Research/Compute → Critic/Verifier → Result → Controlled Memory.

## Layers
Presentation; API; Control Plane; Intelligence; Execution; Model Fabric; Tools; Persistence; Observability; Security.

## Task state machine
CREATED → QUEUED → RUNNING → WAITING_APPROVAL/PAUSED/SUCCEEDED/FAILED/CANCELLED. Transitions are validated and evented.

## Workers
Scheduler → durable lease → bounded worker → execution → heartbeat/events → terminal state. Use fencing/version checks to prevent duplicate execution.

## Supervisor
PLAN → DECOMPOSE → ASSIGN → EXECUTE → TEST → DEBUG/REPAIR → REVIEW → SECURITY → BENCHMARK → ACCEPT → CHECKPOINT → COMMIT/DEPLOY.

## Model Fabric
ModelRequest → candidate filter → capability → verification → policy → context/modality → preference → reliability/latency/cost → inference → ModelResponse → telemetry.

## Provider states
CATALOGUED, REGISTERED, CONFIGURED, DISCOVERED, REACHABLE, VERIFIED, LIVE, BLOCKED, ERROR.

## Memory
Relevant authorized retrieval only; never inject lifetime history blindly.

## Events
Durable events feed tasks, audit, recovery and AI City. Schemas are versioned.

## Scaling
Keep current lightweight architecture modular. Add PostgreSQL, distributed queues, autoscaled workers, object storage and GPU orchestration only when measured load justifies them.
