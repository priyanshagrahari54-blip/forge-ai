# FORGE AI — MEMORY & LEARNING
## Layers
Working/session, conversation, user preference, project, episodic, semantic, procedural, research, operational and agent memory.

## Record
id, scope, type, content, source, confidence, created/updated time, expiration, provenance and retention.

## Write pipeline
Interaction → candidate extraction → privacy/authorization → dedupe → importance/confidence → storage.

## Retrieval
Rank by scope, relevance, recency, confidence and provenance. Retrieve the smallest useful context.

## Contradictions
Preserve conflicting records; do not silently overwrite. Ask the user when a conflict affects a consequential action.

## User controls
Inspect, edit, delete, forget, disable and clear.

## Pattern discovery
Analyze successful/failed tasks, corrections, provider reliability, tool errors and workflow structure. Patterns require evidence and confidence.

## Learning
Allowed: operational routing, prompt, tool-choice and workflow learning. Must not silently override explicit preferences or security.

## Controlled improvement
Observation → hypothesis → candidate → sandbox → benchmark → regression → security review → approval → deployment → monitoring → rollback.

## Model weights
Forge does not claim to train proprietary provider weights. Fine-tuning is infrastructure/provider-specific.

## Privacy
Avoid unnecessary sensitive inference. Enforce user/project/session/task isolation.
