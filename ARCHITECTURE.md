# FORGE AI — MASTER SYSTEM ARCHITECTURE v2

## 0. Mission
Forge is a provider-neutral personal AI engineering/orchestration system. Its core is not a replacement for every mature tool; it is the intelligence, policy, orchestration, verification and integration layer that turns a user request into a verified result.

## 1. Non-negotiable design law
**REUSE → ADAPT → COMPOSE → EXTEND → BUILD.**

Before building a capability, Forge discovers existing libraries, agents, MCP servers, models, engines, APIs and services. It evaluates license, security, maintenance, compatibility, quality, cost and evidence. Exact useful components are reused; Forge-specific behavior is added through adapters, policies and orchestration. New infrastructure is built only when no acceptable verified option exists.

## 2. Global execution pipeline
USER → REQUIREMENT INTELLIGENCE → MASTER CONTRACT → CAPABILITY DISCOVERY → ARCHITECTURE/PLAN → AGENT+MODEL ROUTING → EXECUTION → TEST → DEBUG/REPAIR → SECURITY → BENCHMARK → CONTRACT/QUALITY ACCEPTANCE → CHECKPOINT → DELIVER.

No stage may silently lower the user's requested quality bar.

## 3. Core planes
### Presentation Plane
Chat, voice, cockpit, AI City, project workspace, task/artifact views.
### Control Plane
Sessions, projects, tasks, approvals, policies, event stream, audit and recovery.
### Requirement Plane
Intent parsing, explicit/implicit requirements, constraints, quality bar, references, ambiguities, contradictions, acceptance criteria and failure conditions.
### Intelligence Plane
Prompt enhancement, planning, decomposition, agent selection, model routing, research planning, memory/context assembly and critic/verifier orchestration.
### Capability Plane
External ecosystem discovery, capability registry, adapters, MCP/tool integrations, provider connectors and capability health/truth.
### Execution Plane
Sandboxed workers, browser/computer use, coding, research, media/3D, game/OS tooling and domain execution.
### Verification Plane
Tests, static/dynamic checks, security, benchmark, visual/functional QA, evidence collection and final contract audit.
### Memory/Data Plane
User/project/session/task/agent/research/operational memory with provenance and policy-controlled retrieval.

## 4. Task state
CREATED → CONTRACTED → QUEUED → RUNNING → {WAITING_APPROVAL, PAUSED, REPAIRING} → VERIFYING → {SUCCEEDED, FAILED, CANCELLED}. Every transition is validated and evented.

## 5. Supervisor
PLAN → DECOMPOSE → ASSIGN → EXECUTE → TEST → DEBUG/REPAIR → REVIEW → SECURITY → BENCHMARK → CONTRACT CHECK → ACCEPT/REJECT → CHECKPOINT → COMMIT/DEPLOY.

Acceptance requires evidence. A generated artifact is never proof of completion by itself.

## 6. Agent fleet
The existing 40 × 26 = 1,040 logical-specialist design remains a logical capability fleet, not 1,040 simultaneously running heavyweight models. Agents are selected per subtask and share provider-neutral contracts.

## 7. Model Fabric
ModelRequest → capability filter → provider/model discovery → reachability → verification → policy → context/modality → reliability/latency/cost → selection → bounded execution → telemetry. Provider choice is dynamic; no single provider is structurally required.

## 8. Capability adoption
External capability candidates pass: DISCOVER → INSPECT → LICENSE → SECURITY → QUALITY → COMPATIBILITY → COST/RESOURCE → VERIFY → ADOPT/ADAPT/COMPOSE/EXTEND. Unverified candidates cannot become executable merely by registration.

## 9. Domain adapters
Coding/IDE, browser/computer use, research/RAG, Git/GitHub, Blender/3D, image/video/audio/VFX, game engines, OS/kernel tooling, databases, deployment, model/fine-tuning infrastructure and voice all remain replaceable adapters.

## 10. G560 thin-client principle
The Lenovo G560 is the control/client machine, not an assumed heavy-compute host. Local CPU/RAM/disk use must be minimized. Remote provider/server/compute is selected through provider adapters where legitimately available. Forge must never assume the user will supply a private GPU/server/cloud account.

## 11. Security
Identity → authorization → risk classification → least privilege → sandbox/execution fence → secrets isolation → audit → verification → rollback. Agents cannot grant themselves permissions.

## 12. Scaling
Do not build distributed infrastructure merely for appearance. Keep interfaces replaceable so hosted workers, queues, databases, object storage and additional providers can be added when required and actually available.
