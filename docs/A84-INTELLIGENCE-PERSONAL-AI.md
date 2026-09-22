# A84 — Intelligence & Personal-AI Addendum

The complete addendum (Stages A–R) implemented on top of the existing
FORGE substrate: **one assistant, one memory, one registry — zero parallel
runtimes.** Every stage below names the modules, the contracts, and the
honesty guarantees; every guarantee is exercised by `tests/test_a84_*`.

## Stage-by-stage map

| stage | capability | modules | surface |
|-------|------------|---------|---------|
| A1–A3 | model-class awareness + declared scale | `forge/models/model_class.py`, `registry.py` (`model_class`, `scale`, `scale_band`), `identity.py` (`active_parameter_count`, `architecture`, `model_class`, `modalities`, `scale`) | `forge assistant scale`, `/assistant/models/scale`, cockpit |
| B1–B3 | persistent sessions + continuity | `forge/assistant/sessions.py`, `continuity.py` | `/assistant/sessions*`, `/assistant/continuity` |
| C | personal memory (layered, consent-gated) | `forge/assistant/memory.py` over `forge/memory/` (+ `EPISODIC`/`SEMANTIC`/`PREFERENCE` types) | `/assistant/memory/*`, `forge assistant memory …` |
| D | prompt intelligence | `forge/prompt_intelligence/` (pipeline, quality, strategies, adaptation, versions) | every respond turn; `/assistant/prompts*` |
| E | tool intelligence | `forge/tools/intelligence/` (registry, planner, verification) | `/assistant/tools*` |
| F | deep research | `forge/research/deep.py` over the A81 secure engine | `/assistant/research`, triage `research` |
| G | lawful privacy-network research | `forge/research/networks.py` | `/assistant/networks*` |
| H | pattern layer | `forge/patterns/graph.py` | `/assistant/patterns*` |
| I | learning layers | `forge/learning/` (operational, preferences, routing) + `ModelFabric.attach_learning` | `/assistant/learning` |
| J | cross-model teams | `forge/models/teams.py` | `/assistant/teams/run`, standalone code answers |
| K | critic + verifier + loop | `forge/verification/` | `/assistant/verify`, high-impact turns |
| L | personalization | `forge/assistant/personalization.py` | `/assistant/profile*` |
| M–N | long context + quality | `forge/assistant/context.py` | every assembled prompt |
| O | continuous improvement | `forge/improvement/proposals.py` | `/assistant/improvement/scan` |
| P | runs-as-evidence view | plane executions are the evidence (tasks, orchestrations, agent runs; `/tasks*`, `/orchestrations*`) — no second "run" abstraction was invented | existing views |
| Q | behavior policy | `forge/assistant/behavior.py` | triage on every turn |
| R | honesty matrix | 15 entries in `forge/capabilities/reality.py` + 8 honesty-contract keys | `/capabilities` (A72), `forge assistant status` |

## The pipeline (P1 architecture, as implemented)

```
User → SessionLedger (+ RAM short-term) → ContinuityResolver
     → PromptIntelligence.enhance (preserved terms, ambiguities, intent check)
     → PromptQualityEvaluator  ── EXECUTE | ASK_USER | REJECT
     → AssistantBehavior.triage ── direct_answer | research | code |
        orchestrate | clarify | confirm | recover_continue
     → ContextEngine.build (relevance hierarchy, budgets, lossy digests,
        quality metrics)  → PreferenceProfile (soft hints only)
     → ModelTeam / ModelFabric selection (declared scale + capped priors
        AFTER hard filters)
     → execution, delegated:
          code/orchestrate → ControlPlane.submit_task / submit_orchestration
          research         → DeepResearchEngine → SecureResearchEngine
          answer           → runtime-verified conversation (A43) or honest
                             "no live model" determinstic channel
     → CritiqueLoop (deterministic findings, claim→evidence tracing)
     → AssistantResponse (text + receipts: intent, quality, continuity,
        context quality, tool plan, verification, memory decision)
     → PersonalMemoryService.retain (per-record decision, fail-closed)
     → prompt version + result ledger; session refs; audit stream
```

## Vocabularies (closed sets)

- **Model classes** (closed set, `forge.models.model_class.ModelClass`):
  `transformer_llm`, `multimodal`, `vision`, `audio`, `speech`, `embedding`,
  `reranker`, `reasoning`, `coding`, `long_context`, `tool_use`, `planning`,
  `specialist`. Misdeclared classes are rejected at registration
  (`ValueError`/`IdentityError`), never coerced; "" means *undeclared*.
- **Scale bands** (declared counts only, `_BAND_EDGES`):
  `small` <2B · `medium` <16B · `large` <70B · `xlarge` <200B ·
  `huge` <1T · `massive` ≥1T, and `unknown` whenever not disclosed.
  `parse_declared_count` accepts only explicit disclosures ("70B", "1.2T");
  ranges, "~" and vague labels stay `unknown`. MoE reports total **and**
  active counts when both are disclosed — a 671B-total/37B-active row is a
  *sparse* MoE, and the band never claims 671B of live compute.
- **Tool availability states**: `live | ready | configured | architecture |
  simulated | blocked | missing | error` — the planner refuses
  blocked/missing/error and never upgrades `architecture` to "available".
- **Memory layers**: session, task, project, failure, decision, agent,
  model_performance + preference, episodic, semantic.
- **Triage actions**: direct_answer, research, code, orchestrate, clarify,
  confirm, recover_continue (report-unknown is a reply mode, not an action).

## Decisions that bind the implementation

1. **No disconnected second AI.** All execution flows through the existing
   supervisor, orchestrator, fabric, research engine and A33 policy. The
   assistant plane constructs no thread pools, no sandboxes, no writers.
2. **Memory is consent-shaped.** `consider_retention` defaults to *skip*;
   storage happens on explicit user intent or durable-knowledge markers
   above a usefulness bar. `disabled` retention mode = short-term only.
   Redaction runs at write time through the A37 scanner; secrets never enter
   long-term memory even when the user asks to store them (fail closed).
3. **Conflicts are surfaced, not overwritten.** Same anchor terms + opposite
   polarity → the new record is refused, a `CONFLICT` opens in the pattern
   graph, and the user adjudicates (old-stale / new-stronger /
   context-specific / clarification).
4. **`forget` is real deletion.** Purge of content + vectors + references +
   pattern cleanup, audited, and honestly labeled irreversible. `clear`
   requires the exact confirmation phrase *and* the plane memory gate.
5. **Scale ≠ hosting ≠ availability.** Three separate facts on the scale
   catalog; routing to a trillion-parameter model requires a provider that
   actually offers it. Registration of a model entry never implies capacity.
6. **Personalization can restrict, never relax.** Profile fields are a
   closed vocabulary; permission-shaped keys (`allow_*`) are rejected;
   `prefer_local` only ever *adds* locality; policy denials outrank priors.
7. **Research reports PARTIAL/FAILED/BLOCKED as successes of the process.**
   An empty evidence ledger with named failures is a correct result; a
   confident sentence without citations is not. Web absence never triggers
   model-backfill of "facts".
8. **The networks layer is inert by default.** `enabled=False`, no transport,
   no execution, no downloads, no transactions, host-only audit lines,
   `untrusted` trust for every privacy-network source — and it stays that
   way without an operator-supplied isolated gateway.
9. **Learning proposes; humans and gates apply.** Prior adjustments are
   capped at ±0.05, require ≥10 samples, apply after hard filters, and
   `never_overrides` security/authz/capability/user-choice. Improvement
   output is proposals with evidence + suggested tests + rollback notes; the
   self-modification guard routes anything touching Forge's own code through
   the unchanged A58/A26 loops.
10. **Thin client preserved.** The standalone CLI has no submit_task/orchestrate
    delegates at all — code requests there answer as proposals, explicitly.

## Equivalence ledger (Stage R — what is *not* claimed)

| the shortcut | the truth | where enforced |
|---|---|---|
| "Forge has a trillion-parameter model" | Forge *routes* to provider-offered models; hosting is only what residency proves | `massive-model-routing` reality entry; scale catalog legend |
| "it remembers everything" | retention is per-record, default skip | `consider_retention` tests |
| "the registry means tools work" | descriptions ≠ permissions; availability is probed | catalog honesty note + planner state filters |
| "research found the truth" | corroboration ≥2 distinct URLs or `single_source_claims`; untrusted rows stay untrusted | `test_a84_research` |
| "the dark web layer is live" | gateway disabled, no circuit authority, transport operator-required | networks status endpoint |
| "it learned from our chats" | ledgers propose; weights untouched | learning `governance` block |
| "seven models reviewed it" | same-model review labeled not-independent; unmet roles reported | teams honesty keys |
| "verified answer" | critic `verified` only via claim→evidence tracing; exhausted loops end UNVERIFIED | loop outcome flags |

## Testing

`tests/test_a84_stage_a_models.py` … `test_a84_plane_integration.py` cover
each stage's contracts plus the integrated plane: triage exits (clarify /
confirm / recover), memory refusal/conflict/forget, profile restriction-only
semantics, context budget folding, scale metadata non-invention, priors'
bounded influence, research PARTIAL honesty, networks inert-default, the
reality-matrix additions and the API surface (auth, CSRF, approval gate on
destructive memory ops).
