# FORGE AI — MASTER DOCUMENT INDEX v2

This is the documentation source-of-truth for the current redesign. Code must follow these documents; when implementation and documentation disagree, update the documentation contract first and then reconcile code.

## A. Product and scope
1. `docs/FORGE-MASTER-DOCUMENT-INDEX.md` — this index and document governance.
2. `docs/FORGE-CUSTOMIZATION-CONTRACT.md` — reuse/adapt/compose/extend/build policy and ownership boundary.
3. `ARCHITECTURE.md` — system architecture and execution planes.
4. `DESIGN.md` — UI/UX and truthful state presentation.
5. `AI_CITY.md` — cockpit/AI City contract.

## B. Intelligence
6. `AGENTS.md` — logical agent fleet, lifecycle, selection and evolution.
7. `MODEL_CATALOG.md` — provider/model truth, routing and verification.
8. `MEMORY.md` — memory, RAG, provenance and controlled learning.
9. `forge/core/requirement_intelligence.py` — executable requirement-contract logic.
10. `forge/capabilities/registry.py` + `broker.py` — capability truth and resolution.

## C. Engineering governance
11. `CONTRIBUTING.md` — contribution rules.
12. `.forge/` — project-level machine-readable configuration/contracts where present.
13. Tests under `tests/` — executable acceptance of architecture contracts.

## D. Exact build order
### Phase 0 — Documentation freeze
Requirements, quality bars, constraints, security model, architecture, dependency graph, acceptance rules and ecosystem strategy.

### Phase 1 — Truth/contract core
Requirement Intelligence → Master Contract → Capability Registry → Capability Broker → project/task persistence → acceptance gate.

### Phase 2 — Execution kernel
Supervisor → planner → task state machine → bounded workers → event/audit stream → checkpoint/rollback.

### Phase 3 — Provider/model fabric
Discovery → health/reachability → bounded verification → capability normalization → dynamic model routing → fallback → telemetry.

### Phase 4 — Ecosystem adapters
MCP → coding/IDE → Git/GitHub → browser/computer use → research/search → RAG → databases → deployment. Existing mature systems are adopted/adapted rather than reimplemented.

### Phase 5 — Memory and research intelligence
Source ingestion → provenance → deduplication → indexing → retrieval → evidence synthesis → contradiction detection → project/user memory.

### Phase 6 — Creative and engineering domains
Blender/3D → image/video/audio/VFX → game-engine workflows → OS/kernel workflows → model/fine-tuning workflows. Each is an adapter family, not a reason to bloat the core.

### Phase 7 — Voice and cockpit
Natural voice I/O → dialogue/emotion/context interpretation → safe action policy → AI City state projection → artifacts/observability.

### Phase 8 — Controlled self-improvement
Evaluation corpus → failure analysis → candidate changes → sandbox → benchmark/regression → security review → approved rollout → monitoring → rollback.

### Phase 9 — Production hardening
Security audit, dependency audit, provider truth audit, load/resource tests, disaster recovery, cost/resource policy and final end-to-end acceptance.

## E. Module dependency graph
```text
USER/UI/VOICE
     |
     v
REQUIREMENT INTELLIGENCE
     |
     v
MASTER CONTRACT
     |
     +--------------------+
     |                    |
     v                    v
CAPABILITY REGISTRY    MEMORY/RAG
     |                    |
     v                    v
CAPABILITY BROKER     CONTEXT ASSEMBLER
     |                    |
     +---------+----------+
               v
        PLANNER/DECOMPOSER
               |
       +-------+--------+
       |                |
       v                v
   AGENT ROUTER     MODEL FABRIC
       |                |
       +-------+--------+
               v
        TOOL/ADAPTER LAYER
               |
     +---------+----------+
     |         |          |
   CODE     RESEARCH   CREATIVE/3D
     |         |          |
     +---------+----------+
               v
          EXECUTION
               |
       +-------+--------+
       |       |        |
     TEST   SECURITY  BENCHMARK
       |       |        |
       +-------+--------+
               v
        CONTRACT ACCEPTANCE
               |
        +------+------+
        |             |
       PASS          FAIL
        |             |
     DELIVER      REPAIR/REPLAN
```

## F. Quality doctrine
A task is complete only when: (1) explicit requirements are satisfied, (2) quality target is met, (3) required tests pass, (4) security/permission gates pass, (5) evidence is recorded, and (6) the final result is audited against the original contract.

For a request such as “GTA-like”, “AAA”, “professional Adobe-level”, “full OS” or any other high bar, Forge must not silently produce a cheap prototype and call it complete. If the target cannot be met with available resources/capabilities, it must surface the gap and either continue toward the requested bar or obtain an explicit scope change.

## G. Resource doctrine
The G560 is a thin client. No user-owned server/GPU/cloud is assumed. Remote compute is used only through an actually available provider/service. Zero-rupee is the design target; paid infrastructure cannot be silently assumed.

## H. Ecosystem doctrine
Forge should import/reuse mature components where possible, but customize the integration, policies, memory, routing, verification, security and user experience into a coherent Forge system. Do not copy large projects unnecessarily.
