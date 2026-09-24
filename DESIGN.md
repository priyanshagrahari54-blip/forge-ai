# FORGE AI — UI/UX DESIGN SPECIFICATION
## 1. Goal
Fast, professional, restrained and information-dense without visual noise. The UI must communicate actual system state.

## 2. Navigation
Home, Chat, Tasks, Projects, Agents, Models, Research, Memory, Voice, AI City, Tools, Compute, Git, Security, Approvals, Operations, Settings.

## 3. Home
Show active work, recent results, approvals, health and meaningful metrics. No fake activity.

## 4. Chat
Message history, composer, attachments, voice, task conversion, execution state and artifacts. Distinguish assistant/tool/research/system/approval messages.

## 5. Tasks
Goal, state, stage, agents, models, tools, events, logs, artifacts, verification, controls. Support pause/resume/cancel/retry/inspect.

## 6. Models
Provider, model ID, capabilities, context, discovery state, verification state, latency, reliability and last check. Clearly show configured vs verified vs live.

## 7. Agents
Identity, specialization, capabilities, eligible model pool, tools, memory scope, security profile and evaluation history.

## 8. Research
Question, search plan, sources, evidence, contradictions, confidence and citations.

## 9. Memory
Inspect, edit, delete, forget, disable and clear.

## 10. Voice
States: idle/listening/processing/speaking/approval/error. Normal conversation must not trigger unnecessary yes/no confirmation.

## 11. AI City
Real backend event/state projection only. No procedural fake progress.

## 12. Authentication
Minimal signup/login. Forge workspace is automatic for basic entry; advanced project controls belong in settings.

## 13. Accessibility
Keyboard navigation, focus states, semantic controls, readable contrast, reduced motion and meaningful error text.

## 14. Performance
Lazy-load heavy views, debounce user actions, paginate large lists, avoid unnecessary polling and duplicate requests.
