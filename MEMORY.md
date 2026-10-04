# FORGE AI — MEMORY, RAG & CONTROLLED LEARNING v2

## 1. Memory layers
Working/session, conversation, user preference, project, episodic, semantic, procedural, research, operational and agent memory.

## 2. Record contract
Every durable record has ID, scope, type, content/reference, source, confidence, timestamps, provenance, retention policy and authorization boundary.

## 3. Retrieval
Rank by authorization, scope, relevance, recency, confidence and provenance. Retrieve the smallest useful context. Never dump lifetime memory into a model context.

## 4. Knowledge/RAG ingestion
Forge may ingest permitted information from repositories, documentation, websites, research sources, courses, videos/transcripts and other accessible sources. Acquisition must respect access controls, licensing/terms, privacy and source provenance. Raw ingestion is not automatically trusted knowledge.

## 5. Knowledge verification
Source → parse → normalize → deduplicate → provenance → quality/reliability assessment → contradiction detection → index → retrieval → evidence-backed answer. Low-confidence or conflicting information remains marked as such.

## 6. Contradictions
Keep conflicting records visible. Prefer verified/newer records only when policy permits; ask the user when a conflict materially affects consequential action.

## 7. User controls
Inspect, edit, delete, forget, disable retention and clear.

## 8. Learning
Operational learning may improve routing, prompts, tool choice, retrieval, agent selection and workflow proposals. It must never silently override explicit user preferences, safety/security policy or the requirement contract.

## 9. Self-improvement
Observation → hypothesis → candidate change → isolated sandbox → benchmark → regression → security review → approval policy → deployment → monitoring → rollback. Self-improvement is controlled engineering, not uncontrolled self-rewriting.

## 10. Models and fine-tuning
Forge can orchestrate fine-tuning/model-building workflows through available infrastructure and providers. It must not claim to have trained proprietary provider weights or have compute that is not actually available.

## 11. Privacy/security
No unnecessary sensitive inference. Enforce user/project/session/task/agent isolation, source permissions and retention policy.
