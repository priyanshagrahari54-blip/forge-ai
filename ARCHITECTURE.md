# FORGE AI — SYSTEM ARCHITECTURE
## 1. High-level
User → Chat/Voice/Cockpit → API/Auth → Control Plane → Task/Supervisor → 1,040 specialists → Model Fabric → verified providers/models → Tools/Research/Compute → Critic/Verifier → Result → controlled Memory.

## 2. Layers
Presentation: UI, voice, AI City.
API: schemas, auth, rate limits, stable errors.
Control Plane: sessions, tasks, approvals, events, projects.
Intelligence: intent, prompt intelligence, planner, decomposition, specialist selection.
Execution: workers and tools.
Model Fabric: registry, discovery, capability mapping, verification, routing, fallback, telemetry.
Persistence: repositories over SQLite today; PostgreSQL-compatible abstractions for future scale.

## 3. Task state machine
CREATED → QUEUED → RUNNING → {WAITING_APPROVAL, PAUSED, SUCCEEDED, FAILED, CANCELLED}. Resume returns to RUNNING/QUEUED according to policy. Every transition is validated and evented.

## 4. Worker model
Persistent task → scheduler → lease → bounded worker pool → execution → heartbeat/events → terminal state. Prevent duplicate execution with lease/version fencing.

## 5. Supervisor
PLAN → DECOMPOSE → ASSIGN → EXECUTE → TEST → DEBUG/REPAIR → REVIEW → SECURITY → BENCHMARK → ACCEPT → CHECKPOINT → COMMIT/DEPLOY.

## 6. Model Fabric
ModelRequest → candidates → capability filter → verification → policy → context/modality → preference → reliability/latency/cost → provider inference → ModelResponse → telemetry.

## 7. Event architecture
Durable events power task UI, AI City, audit, recovery and debugging. Event schemas are versioned.

## 8. Memory
Context assembly retrieves only relevant authorized memories. Never dump lifetime memory into prompts.

## 9. Security
Authentication → authorization → risk classification → approval where required → execution fence → audit → verification → rollback.

## 10. Scaling
Do not add distributed infrastructure prematurely. Keep service boundaries replaceable so queues, PostgreSQL, object storage and autoscaled workers can be introduced when measured load requires them.
