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
    search providers (SearXNG instance or OpenAI). Every result
    carries provenance (`REAL_SEARCH_RESULT` vs `MODEL_KNOWLEDGE`);
    only genuine provider search output is presented as a web
    result, model knowledge stays visibly labeled as unverified
    external input.
  - **`fetch_url(url)`** — fetch and extract text from a URL.
    Content is bounded and labeled untrusted.
- `forge/research/web.py` — **Real web search providers**:
  - `WebSearchProvider`: SearXNG instance (`FORGE_SEARXNG_URL`) and
    OpenAI Responses API (`web_search` tool), with a clearly-labeled
    ChatCompletion knowledge fallback for generic provider errors
    (never on auth/rate-limit/timeout failures).
  - `fetch_page_content()` for bounded URL text extraction.
  - Requires `OPENAI_API_KEY` or `FORGE_SEARXNG_URL` (for search;
    URL fetch itself is key-free).
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
| `OPENAI_API_KEY` | Enables the OpenAI web-search provider |
| `FORGE_SEARXNG_URL` | SearXNG instance URL for privacy-respecting search |
| `FORGE_SEARXNG_ALLOW_HTTP` / `FORGE_SEARXNG_ALLOW_PRIVATE` | Explicit operator opt-ins for an `http://` or private-network SearXNG instance (fail-closed by default) |

## Security notes

- Research reads the project the session is bound to, via the same
  intelligence index the supervisor and conversation engine already
  use; answers leak nothing outside the project and cite paths only
  from the index.
- Web results are untrusted external input by construction, and
  provenance is explicit: `REAL_SEARCH_RESULT` only for genuine
  provider output (SearXNG hits, OpenAI `web_search_call` items);
  `MODEL_KNOWLEDGE` for knowledge fallbacks and text-only answers.
  An empty successful result set is reported honestly ("no results")
  — never padded with invented results.
- Every user/agent URL and every redirect hop goes through the
  SSRF-safe chain (`forge.security.ssrf`): scheme, hostname, DNS
  resolution of all addresses, IP classification, port, content-type,
  and size checks before a byte is downloaded. OpenAI API POSTs use
  the compile-time `https://api.openai.com` host over verified TLS
  with redirects refused (a 3xx is an error, never a silent
  scheme/host change).
- No answer is ever synthesized from thin air.

## Testing

`tests/test_a47_research.py`: symbol questions with real file:line
evidence, dependency and test-coverage questions, honest no-evidence
refusal, real report counts, plane audit records, API boundaries,
cockpit contracts.

`tests/test_a47_research_web.py` (offline, no secrets): provenance
labels — SearXNG and OpenAI real hits are `REAL_SEARCH_RESULT`,
text-only answers and the chat fallback are `MODEL_KNOWLEDGE`;
no knowledge fallback on auth/rate/timeout/unavailable failures;
empty success stays honest; OpenAI POST redirects refused; fetch
outcome classification (success/blocked/timeout/http error);
engine evidence provenance and honest no-results answers.
