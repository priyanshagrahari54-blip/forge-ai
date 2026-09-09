# A47 — Research / Intelligence

Evidence-based research over the codebase + real Internet search:
questions are answered only from what the repository intelligence
index can prove (with citations) — or from live web search results
when configured. Answers are honestly refused when nothing supports them.

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
  - **`ask_web(question)`** — real Internet search via configured
    search providers (OpenAI, SearXNG, DuckDuckGo). Results are
    labeled as untrusted external input.
  - **`fetch_url(url)`** — fetch and extract text from a URL.
    Content is bounded and labeled untrusted.
- `forge/research/web.py` — **Real web search providers**:
  - `WebSearchProvider` with OpenAI Responses API (web_search tool),
    SearXNG instance, and OpenAI ChatCompletion fallback.
  - `fetch_page_content()` for bounded URL text extraction.
  - Requires `OPENAI_API_KEY` or `FORGE_SEARXNG_URL`.
- Control plane `research_ask()` / `research_report()` / **`research_web()`** /
  **`research_fetch()`** — project-bound, audited under `research`.
- API: `POST /api/v1/research/ask` (rate-limited), `GET
  /api/v1/research/report`, **`POST /api/v1/research/web`**,
  **`POST /api/v1/research/fetch`**.
- Cockpit: Research view (nav + palette + template) with the usual
  UI contracts — renderer calls only `/api/v1/research/*`.

## Configuration

| Variable | Effect |
|---|---|
| `OPENAI_API_KEY` | Enables OpenAI web search + URL fetch |
| `FORGE_SEARXNG_URL` | SearXNG instance URL for privacy-respecting search |

## Security notes

- Research reads the project the session is bound to, via the same
  intelligence index the supervisor and conversation engine already
  use; answers leak nothing outside the project and cite paths only
  from the index.
- Web results are untrusted external input by construction.
- No answer is ever synthesized from thin air.

## Testing

`tests/test_a47_research.py` (8): symbol questions with real
file:line evidence, dependency and test-coverage questions, honest
no-evidence refusal, real report counts, plane audit records, API
boundaries, cockpit contracts.

A47 result: **8 new tests; full suite 1253 passed, 2 skipped** (A46
baseline: 1245 passed, 2 skipped).
