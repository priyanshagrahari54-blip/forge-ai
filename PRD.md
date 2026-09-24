# FORGE AI — Product Requirements Document
Version: Master v1.0

## 1. Mission
Forge AI is a server-side personal AI and engineering orchestration platform. It must turn natural requests into truthful, permissioned, verifiable outcomes.

## 2. Product principles
- Real execution over UI simulation.
- User intent and control remain authoritative.
- Heavy inference/compute stays server/provider-side.
- Logical agents are roles, not 1,040 permanent model processes.
- Model availability is proven, never invented.
- Important results are independently verified.
- Memory is controlled, inspectable and deletable.

## 3. Core journeys
### Conversation
Input → session/context → model routing → response → optional memory proposal.
### Task
Input → intent → policy → task → scheduler → worker → agent/model/tool → verification → result.
### Complex engineering
Request → requirements → plan → DAG → specialists → implementation → tests → debug → security → review → benchmark → artifact.
### Research
Question → scope → search → sources → evidence → contradiction analysis → synthesis → citations.
### Voice
Speech → STT → intent → policy → answer/action → TTS.
### Recovery
Failure → classify → retry/fallback/replan → checkpoint/rollback → truthful status.

## 4. Functional requirements
FR-01 Authentication and sessions.
FR-02 Persistent task execution.
FR-03 Durable task states and recovery.
FR-04 Model registry and dynamic discovery.
FR-05 Real inference verification.
FR-06 Capability-aware model routing.
FR-07 1,040 logical specialists (40×26).
FR-08 Multi-agent orchestration.
FR-09 Permissioned tool execution.
FR-10 Personal memory.
FR-11 Prompt intelligence.
FR-12 Deep research with provenance.
FR-13 Voice.
FR-14 Vision/computer use.
FR-15 AI City based on real events.
FR-16 Evaluation and controlled improvement.
FR-17 Observability and rollback.

## 5. Non-functional requirements
Reliability: no silent task loss; recoverable worker failures.
Security: least privilege, secret isolation, fail-closed authorization.
Performance: measurable queue, routing, provider and E2E latency.
Maintainability: modular contracts and regression tests.
Truthfulness: simulated/configured/verified/live states remain distinct.
Resource efficiency: weak client remains a thin client.

## 6. Acceptance
A feature is complete only after implementation, tests, integration, runtime verification, security verification, observability and documentation.
