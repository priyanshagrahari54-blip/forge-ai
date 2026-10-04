# Forge Ecosystem Adoption Strategy

Forge is not a clone of one agent framework. It is a custom control plane that adopts mature capabilities behind stable Forge contracts.

## Current ecosystem candidates

- Long-running/stateful orchestration: evaluate LangGraph and Microsoft Agent Framework.
- Software engineering: evaluate OpenHands SDK/runtime.
- Browser/computer use: evaluate Browser Use and Playwright.
- MCP/integration: use the MCP ecosystem through Forge's provider/tool boundary.
- RAG/retrieval, memory, evaluation and observability: select specialized mature components rather than recreating each subsystem.

## Selection rule

A candidate must pass security, license, maintenance, compatibility, quality and integration checks before becoming usable. Discovery alone never activates code.

## Forge-owned differentiation

Forge keeps ownership of:

1. Requirement Intelligence and the Master Contract.
2. Capability truth and evidence.
3. Provider/model routing policy.
4. Agent fleet composition and task decomposition.
5. Permission boundaries and sandbox policy.
6. Cross-domain project state and memory policy.
7. Quality gates, acceptance and repair loops.
8. Auditability, rollback and user approval.
9. Thin-client/resource policy for constrained machines.
10. A unified user-facing AI operating layer.

## Important constraint

"Import" means reuse through a pinned dependency, adapter, subprocess/service boundary, MCP server, or compatible upstream interface. It does not mean copying an entire repository into Forge without license, security and maintenance review.

## First integration wave

1. Agent/workflow runtime adapter.
2. Coding-agent adapter.
3. Browser/computer-use adapter.
4. MCP capability discovery.
5. Retrieval/RAG adapter.
6. Evaluation/observability adapter.
7. Creative/3D adapters.

Each adapter must expose health, version, permissions, provenance and verification evidence to Forge.
