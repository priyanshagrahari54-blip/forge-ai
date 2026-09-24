# FORGE AI — UI/UX MASTER DESIGN
## Design goal
Professional, clean, fast, information-rich and low-resource. The UI must display actual backend state.

## Navigation
Home, Chat, Tasks, Projects, Agents, Models, Research, Memory, Voice, AI City, Tools, Compute, Git, Security, Approvals, Operations, Settings.

## Home
Active work, recent results, approvals and health. No fake metrics.

## Chat
History, composer, attachments, voice, task conversion, progress, artifacts and distinct assistant/tool/research/system/approval/error messages.

## Tasks
Goal, state, stage, agents, models, tools, events, logs, artifacts, verification and pause/resume/cancel/retry/inspect controls.

## Models
Provider, model ID, capabilities, context, discovery state, verification state, latency, reliability and last verification. Never hide the distinction between configured/discovered/verified/live.

## Agents
Identity, specialization, capabilities, eligible model pool, tools, memory scope, security profile and evaluation.

## Research
Question, sources, evidence, contradictions, confidence and citations.

## Memory
Inspect, edit, delete, forget, disable and clear.

## Voice
IDLE/LISTENING/PROCESSING/SPEAKING/WAITING_APPROVAL/ERROR. Harmless conversation must not trigger unnecessary yes/no prompts.

## AI City
Only real events/state. Every activity should be traceable to task/run/event identity.

## Authentication
Minimal signup/login. Basic Forge workspace is automatic; advanced project controls belong in settings.

## Accessibility/performance
Keyboard navigation, focus states, semantic controls, reduced motion, lazy loading, pagination, debounced actions and minimal duplicate API calls.
