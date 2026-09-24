# FORGE AI — MEMORY & LEARNING SPEC
## Layers
Working/session, conversation, user preference, project, episodic, semantic, procedural, research, operational and agent memory.

## Record
ID, scope, type, content, source, confidence, timestamps, expiration, provenance and retention policy.

## Write pipeline
Interaction → candidate extraction → privacy/authorization → dedupe → importance → confidence → storage.

## Retrieval
Rank by scope, relevance, recency, confidence and provenance. Retrieve the smallest useful context.

## Contradictions
Keep conflicting records visible. Prefer verified/newer records only when policy permits; ask user when conflict affects consequential action.

## User controls
Inspect, edit, delete, forget, disable retention and clear.

## Pattern discovery
Use task outcomes, corrections, provider reliability, tool failures and workflow structure. Patterns require evidence and confidence.

## Learning
Operational learning may improve routing, prompts, tool choice and workflow proposals. It must not silently override explicit user preferences or security.

## Controlled improvement
Observation → hypothesis → candidate → sandbox → benchmark → regression → security review → approval → deployment → monitoring → rollback.

## Model weights
Forge does not claim to train proprietary provider weights. Fine-tuning is provider/infrastructure-specific.

## Privacy
No unnecessary sensitive inference. Enforce user/project/session/task isolation.
