# Forge Customization Contract

Forge is customized as a **Universal AI Orchestrator + Quality Authority**, not as a
replacement for every mature tool in the ecosystem.

## Non-negotiable build rule

**REUSE > ADAPT > COMPOSE > EXTEND > BUILD**

For every missing capability, Forge should:

1. discover existing implementations and integrations;
2. compare license, security, maintenance, compatibility, quality, cost and
   integration effort;
3. prefer an existing verified capability;
4. adapt or compose before writing new infrastructure;
5. build from scratch only when no acceptable verified option exists.

## Core responsibilities

Forge owns the parts that must remain consistent across every domain:

- requirement intelligence and the Master Contract;
- orchestration and dependency planning;
- model/agent/tool routing;
- permissions and sandboxing;
- project isolation and memory policy;
- evidence collection;
- testing and verification;
- final quality audit and delivery decision;
- rollback and auditability.

## Capability responsibilities

Domain capabilities should normally live behind adapters/providers:

- coding and IDE tooling;
- browser/computer use;
- research and retrieval;
- model providers;
- databases;
- Git/GitHub;
- Blender/3D;
- image/video/audio/VFX;
- game engines;
- OS build/debug tooling;
- deployment providers.

Forge must not claim that an adapter is live merely because a package, URL,
model name, or manifest exists.

## Requirement contract

Every complex task is represented by a contract containing:

- user intent;
- explicit and implicit requirements;
- quality requirements;
- references;
- constraints and available resources;
- capabilities needed;
- acceptance criteria;
- failure conditions;
- verification methods;
- unresolved ambiguity/contradiction.

An unresolved quality ambiguity is visible to the planner and must not be silently
converted into a lower quality target.

## Delivery rule

A task is not complete because code was generated.

Delivery requires:

**requirements satisfied + tests passed + security gates passed + quality audit
passed + evidence recorded**

If the requested quality bar cannot realistically be reached with the available
capabilities/resources, Forge must say so and propose a scoped alternative rather
than secretly downgrading the result.

## Thin-client principle

The Lenovo G560 is treated as a lightweight client/control surface. Forge should
minimize local CPU/RAM/disk/network work and use legitimately available remote
providers or hosted services through adapters when available. No hidden server,
GPU, paid API, or user-managed infrastructure may be assumed.

## What is deliberately not built into Forge

Forge should not reinvent:

- mature agent frameworks;
- mature RAG/vector/search engines;
- browsers;
- media/3D/game engines;
- model runtimes;
- generic databases;
- generic deployment platforms.

Only the integration, policy, verification, and orchestration layer belongs in
Forge unless a missing capability is genuinely unavailable elsewhere.


## Canonical 2026-10-05 Update
- **Dynamic agent fabric:** the historical 40×26/1,040 taxonomy is only a seed catalogue. Runtime agent count is demand-driven: Forge creates/selects as many logical specialists as the task graph requires and releases temporary specialists when their work is complete, subject to real resource/concurrency limits.
- **Build-everything principle:** REUSE > ADAPT > COMPOSE > EXTEND > BUILD. Forge may build missing capability from scratch when verified ecosystem components are inadequate; reuse is an optimization, not a prohibition on building.
- **Large-model support:** the model fabric supports small/local through 100B+ parameter-class hosted models when a real provider exposes them. Parameter count never proves availability; reachability, quota, capability, quality and policy verification are mandatory.
- **Free-resource strategy:** prefer legitimately available free/zero-cost/local capacity when it meets the contract. Runtime quota/credit/availability checks are mandatory; unlimited-free capacity is never assumed and money is never spent silently.
- **High-capability security/reverse engineering:** authorized pentesting, CTF/lab work, vulnerability research, binary/protocol analysis, fuzzing, isolated malware analysis, OS/kernel security and interoperability/reverse engineering are supported through scoped authorization, sandboxing, least privilege, network controls, audit and rollback.
- **Ecosystem:** Hugging Face, GitHub/open source, MCP, model providers, package registries and specialist tools are capability sources. External code/data is inspected and verified before executable adoption.
- **Truth rule:** no fake model, agent, tool, compute or completion state. Every capability must have evidence of actual availability before it is presented as live.
