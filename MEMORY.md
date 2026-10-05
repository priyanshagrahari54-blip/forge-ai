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


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
