# A81 — Secure Research Engine

`forge research` lets Forge research technical topics, APIs,
documentation, libraries, errors, and project-specific questions from
multiple sources — with **explicit provenance on every result** and a
fail-closed network policy.

## Sources

| Source | Provenance | What it reads |
|---|---|---|
| `user_provided` | `USER_PROVIDED` | `--note` text and `--file` paths (inside the project) |
| `project_files` | `LOCAL_SOURCE` | Symbols from the repository intelligence index + bounded text search over source files |
| `local_docs` | `LOCAL_SOURCE` | `docs/`, README, CHANGELOG… (`doc_paths`) |
| `repository_metadata` | `LOCAL_SOURCE` | `pyproject.toml` / `requirements*.txt` / `package.json` dependencies, entry points, `requires-python`, git remotes/branch (credentials stripped) |
| `official_docs` | `REAL_WEB_RESULT` | Official documentation for libraries detected in the question (built-in HTTPS index + `official_docs` config; stdlib → docs.python.org) |
| `configured_web` | `REAL_WEB_RESULT` | Operator-configured search templates / pages (`web_sources`) |
| `model_knowledge` | `MODEL_KNOWLEDGE` | Optional model callable — **explicit opt-in only** |

Local sources skip `.git`, `.forge`, virtualenvs, build output, `.env*`,
key/credential files and binaries, never follow symlinks, never read
outside the project root, and drop lines that look like credential
assignments from snippets.

## Provenance contract

Every `ResearchResult` carries one of:

- `LOCAL_SOURCE` — read from the project on disk (trusted, cited as `path:line`)
- `REAL_WEB_RESULT` — bytes actually downloaded through the SSRF-safe chain (cited as URL + `retrieved_at` + HTTP status; untrusted)
- `USER_PROVIDED` — operator notes/files for this query (untrusted)
- `MODEL_KNOWLEDGE` — model output without a verifiable source (unverified)

**Failed web research is never silently replaced by model knowledge.**
A web source that times out, is refused by policy, or errors is reported
in `sources[]` with its state (`TIMEOUT` / `POLICY_DENIED` / `ERROR`),
`web_failed: true` is set, and the answer says so. The model source runs
only when `--allow-model-knowledge` (or `allow_model_knowledge: true`)
*and* a model callable is configured; its results are labeled, ranked
below all verified evidence, and counted in `model_knowledge_used`.

## Pipeline

```
question → QueryPlanner (intent, terms, identifiers, error names, libraries,
           sub-queries, source order)
        → sources (local first; web only when enabled)   [cache for web]
        → rank_results (relevance × provenance weight + source/kind bonuses)
        → deduplicate (per-provenance locator + content fingerprint)
        → summarize (extractive; every sentence cites [n] path:line / URL)
```

Ranking, dedup, summarization and planning are pure, deterministic
functions (`forge/research/secure_engine.py`, `forge/research/query_planner.py`).

## Security

Every web fetch goes through `forge.security.ssrf.fetch` with a policy
derived from the research config:

- **HTTPS by default** (`allow_http: false`); scheme allowlist `https`/`http` only
- **Host allowlist**: only configured `web_sources` hosts + official-docs hosts are contacted; URLs in the question are fetched only if their host is on the allowlist
- **Blocked**: `localhost`/loopback, private (RFC1918, ULA), link-local & cloud metadata (`169.254.0.0/16`, `metadata.google.internal`, …), CGNAT, multicast/reserved, internal DNS suffixes, IPv4-mapped/6to4 IPv6, bare numeric hosts, embedded credentials, local-service ports
- **DNS**: every resolved address is classified; connection is pinned to the validated IP (no rebinding window)
- **Redirects**: every hop re-validated through the same chain; hard cap (`max_redirects`, ≤5)
- **Timeout** per hop (`timeout_seconds`, 1–60) and **response size limit** (`max_bytes`, ≤2 MB), content-type allowlist, identity encoding only
- Defense in depth: the web sources pre-validate URL + IP literals before any fetcher (even an injected one) sees them
- Config validation is fail-closed: invalid/unsafe entries are dropped and listed in `rejected`, never widening policy

## Cache

`ResearchCache` (memory + `.forge/research_cache/`, git-ignored) stores
only `SUCCESS` web outcomes, with a TTL (`cache_ttl_seconds`) and a size
bound (`cache_max_entries`). Failures/denials are never cached. Cached
outcomes are reported as `CACHED`. `forge research cache-clear` empties it.

## Configuration — `.forge/research.yaml`

```yaml
web_enabled: true
allow_http: false
timeout_seconds: 10
max_bytes: 200000
max_redirects: 3
cache_ttl_seconds: 21600
allow_model_knowledge: false
doc_paths: [docs, README.md]
web_sources:
  - name: python-docs
    url: "https://docs.python.org/3/search.html?q={query}"
official_docs:
  fastapi: "https://fastapi.tiangolo.com/"
```

## Integration

- **Planner**: `forge.research.integration.ResearchAwarePlanner` wraps the
  core `Planner`, inserting step `1a` ("Research the requirement using
  cited evidence: …") between steps 1 and 2. The base planner is unchanged.
- **Context engine**: `research_context_items()` / `enrich_context_with_research()`
  turn `LOCAL_SOURCE` results into `ContextItem`s (kind `research`, repo
  files only); `external_notes()` exposes web/user/model results as
  labeled untrusted notes — they are never added as file context.
- **Control plane / API**: `ControlPlane.research_query()` / `research_status()`
  (audited under `research`), `POST /api/v1/research/query`,
  `GET /api/v1/research/status`.

## CLI

```bash
forge research                       # status: sources, policy, cache
forge research query "httpx ReadTimeout when calling fastapi endpoint"
forge research query "where is stage_files defined" --no-web --json
forge research query "…" --source local_docs --note "prod uses 5 retries"
forge research query "…" --allow-model-knowledge   # labeled, opt-in
forge research plan "…"              # show the query plan only
forge research cache-clear
```

Exit code is `1` when web research failed *and* nothing else was found.

## Testing

`tests/test_research_secure_engine.py` runs fully offline (an autouse
fixture makes any DNS/socket call fail): provenance labels, honest web
failure with no model backfill, opt-in model knowledge, SSRF refusal of
metadata/loopback/http/internal/port targets, allowlist enforcement for
question URLs, config validation, cache TTL/bounds/no-failure caching,
ranking/dedup/summary, local secret and path-escape safety, planner and
context integration, control plane audit, API, and the CLI.
