# A47 — Research / Intelligence

Evidence-based research over the codebase: questions are answered
only from what the repository intelligence index can prove, with
citations — and honestly refused when nothing supports an answer.

## What A47 adds

- `forge/research/engine.py` — `ResearchEngine` over the existing
  `RepositoryIntelligence` stack (symbols, dependency graph, test
  mapping, architecture):
  - `ask(question)` — identifier extraction, then evidence search
    across symbols (file:line citations), dependencies ("what
    imports X?", "what does X import?"), and test coverage ("which
    tests cover X?"). Every evidence item is a real indexed path;
    answers are assembled only from found evidence.
  - **Honest refusal**: no evidence → "I found no evidence... I
    will not guess" with `confidence: 0.0`. Confidence is a
    documented evidence-coverage heuristic, not a probability.
  - `report()` — real repository facts (source/test/package counts,
    entry points, layers, config files, cycles) marked
    `evidence_based: true`.
- Control plane `research_ask()` / `research_report()` — project-bound
  (engine built over the session's project root), audited under
  `research`.
- API: `POST /api/v1/research/ask` (rate-limited), `GET
  /api/v1/research/report`.
- Cockpit: Research view (nav + palette + template) with the usual
  UI contracts — renderer calls only `/api/v1/research/*`.

## Security notes

- Research reads the project the session is bound to, via the same
  intelligence index the supervisor and conversation engine already
  use; answers leak nothing outside the project and cite paths only
  from the index.
- No answer is ever synthesized from thin air.

## Testing

`tests/test_a47_research.py` (8): symbol questions with real
file:line evidence, dependency and test-coverage questions, honest
no-evidence refusal, real report counts, plane audit records, API
boundaries, cockpit contracts.

A47 result: **8 new tests; full suite 1253 passed, 2 skipped** (A46
baseline: 1245 passed, 2 skipped).
