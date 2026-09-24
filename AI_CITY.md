# FORGE AI — AI CITY
## Purpose
AI City is an operational visualization of actual Forge state, not an animation engine.

## Districts
Command, Engineering, Research, Models, QA, Security, Memory, Tools, Voice and Compute.

## Event inputs
agent.selected, model.selected, task.started, task.stage_changed, tool.started, tool.completed, tests.running, security.running, task.completed, task.failed and rollback.completed.

## State projection
Durable events → city state projection → UI. If no real event exists, show idle/unknown rather than fabricate activity.

## UI
Agent/model/task nodes, status indicators, drill-down, filters, timelines and error inspection.

## Acceptance
A displayed activity must be traceable to a real task/run/event ID.
