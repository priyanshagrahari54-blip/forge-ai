# FORGE AI — PROJECT STARTUP PACK v2

Use this pack before starting any major Forge-generated project.

## 1. Project brief
- user goal
- target users
- platforms
- inputs/outputs
- constraints
- dependencies
- required quality level

## 2. Requirement contract
- explicit requirements
- implicit requirements
- non-goals
- references/examples
- acceptance criteria
- failure conditions
- ambiguities
- contradictions

## 3. Feasibility/resource sheet
- available compute
- available providers
- available tools
- storage/network constraints
- licensing/access constraints
- expected runtime/cost
- unavailable assumptions

## 4. Architecture decision record
- chosen architecture
- alternatives considered
- reason for selection
- external components adopted
- custom Forge components
- security boundaries
- rollback strategy

## 5. Capability plan
For each required capability: DISCOVER → VERIFY → REUSE/ADAPT/COMPOSE/EXTEND/BUILD → TEST → REGISTER.

## 6. Agent plan
- required specialists
- subtasks
- model pools
- tool permissions
- memory scopes
- evaluation criteria

## 7. Data/RAG plan
- sources
- permissions/license
- ingestion method
- provenance
- deduplication
- retrieval
- evaluation
- retention

## 8. Security plan
- threat model
- trust boundaries
- secrets
- permissions
- sandbox
- network policy
- dependency policy
- audit logging
- rollback

## 9. Test/quality plan
- unit tests
- integration tests
- end-to-end tests
- visual/UX tests where relevant
- performance tests
- security tests
- benchmark/reference comparison
- final contract audit

## 10. Delivery checklist
- requirement contract satisfied
- no unresolved mandatory ambiguity
- requested quality bar satisfied
- evidence collected
- tests pass
- security passes
- artifact reproducible
- limitations disclosed
- rollback/checkpoint available

## 11. Vibe-coding rule
Vibe coding may accelerate implementation, but generated code is treated as a candidate implementation. Forge must inspect, test, secure, verify and integrate it before delivery. “It generated successfully” is never an acceptance criterion.


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
