# FORGE AI — UI/UX DESIGN SPECIFICATION v2

## 1. Goal
Fast, professional, information-dense and truthful. The UI must show actual system state, not simulated activity or fake completion.

## 2. Core navigation
Home, Chat, Tasks, Projects, Agents, Models, Research, Knowledge/RAG, Memory, Voice, AI City, Tools/Capabilities, Git, Security, Approvals, Operations and Settings.

## 3. Requirement-first interaction
When a request is complex, the UI shows the interpreted requirement contract: goals, constraints, quality bar, references, required capabilities, ambiguities, acceptance criteria and verification plan. User can inspect/approve before execution where policy requires.

## 4. Home
Active work, recent results, approvals, health, capability/provider truth and meaningful metrics. No fake progress.

## 5. Tasks
Goal, contract, state, stage, agents, models, capabilities, tools, events, logs, artifacts, tests, security, benchmarks, evidence and final acceptance. Support pause/resume/cancel/retry/inspect.

## 6. Models
Provider, model ID, capabilities, context, modality, discovery/reachability/verification/live state, latency, reliability, cost metadata and limitations. Clearly distinguish configured, reachable, verified and live.

## 7. Agents
Identity, specialization, capabilities, eligible model pool, tools, memory scope, security profile, provenance and evaluation history.

## 8. Capability marketplace/registry
Show source, version, license/access class, security status, quality evidence, compatibility, cost, strategy (reuse/adapt/compose/extend/build) and health. Never present an unverified integration as available.

## 9. Research/RAG
Question, search plan, sources, evidence, provenance, contradictions, confidence, retrieved context and citations.

## 10. Memory
Inspect, edit, delete, forget, disable and clear. Show scope and provenance.

## 11. Voice
Idle/listening/processing/speaking/approval/error. Natural conversation should not trigger unnecessary command-like confirmation. Emotion/context signals are inputs to dialogue policy, not permission to perform risky actions.

## 12. AI City
Real backend event/state projection only. No procedural fake progress.

## 13. Quality gate
Final result view must show requirement satisfaction, tests, security, benchmark/quality checks, unresolved limitations and evidence before claiming success.

## 14. Accessibility/performance
Keyboard navigation, focus states, semantic controls, readable contrast, reduced motion, lazy loading, pagination, debouncing and minimal unnecessary polling/network traffic.
