# A81 — Long-Term Memory

Forge remembers useful project information across tasks and sessions —
without ever storing secrets.

## What A81 adds

A production-grade SQLite-backed memory system under `forge/memory/`:

- **Seven memory layers** (`forge.memory.types.MemoryType`): `session`,
  `task`, `project`, `failure`, `decision`, `agent`, and
  `model_performance`.
- **Core engine** (`forge.memory.engine.LongTermMemory`): typed,
  project-scoped, timestamped records. Every item carries `type`,
  `project`, `timestamp` (`created_at`/`updated_at`/`last_accessed_at`),
  `source`, `via`, `confidence`, `importance`, and a
  `retention`/`expires_at` policy (`ephemeral`/`session`/`task`/
  `project`/`persistent`, with deterministic TTLs).
- **Relevance retrieval** (`forge.memory.relevance`): lexical IDF
  ranking blended with recency decay, importance, confidence, and a
  per-type prior — deterministic, no embeddings, no model calls.
- **Flood prevention**: a signal floor rejects stop-word noise and
  sub-threshold content; per-project/per-type caps evict the least
  important, oldest items first.
- **Duplicate prevention**: exact-fingerprint and bounded
  Jaccard near-duplicate detection merges repeats into the original
  with a recorded provenance event.
- **Summarization** (`forge.memory.summarizer`): deterministic
  *extractive* summaries (never invented prose) compact recent items
  into one durable record.
- **Correction**: `correct()` supersedes the original with a new
  version (version bump, `supersedes` link) — the audit trail is never
  destroyed.
- **Deletion**: soft `delete()` (auditable) and hard `purge()` (with a
  surviving provenance trail), plus `enforce_retention()` and
  `purge_expired()`.
- **No secrets** (`forge.memory.redaction`): every candidate is scanned
  before persistence. Secret spans (API keys, passwords, tokens,
  private keys, DB credentials, JWTs, bearer headers) are redacted in
  place with a persisted `redaction_count`; content that is *nothing
  but* a secret is refused outright.
- **Provenance**: an append-only `long_term_memory_provenance` table
  records every create/correct/supersede/merge/summarize/delete/purge/
  expire event with actor and detail.

## Integrations (all optional and additive)

- **Planner** — `Planner.create_plan(..., memory=, project=)` prepends
  a recall step surfaced from relevant memory.
- **Context engine** — `AgentContextBuilder.build(..., memory=,
  project=)` injects `kind="memory"` context items.
- **Debugger** — `TestDebugLoop.run(..., memory=, project=)` records
  bounded, redacted failure signatures as failure memory.
- **Reviewer** — `ReviewerAgent.review(..., memory=, project=)`
  records HIGH/CRITICAL findings as durable decision memory.
- **Model router** — `ModelFabric.attach_memory(memory, project=)`
  records every routed-call outcome as model-performance memory
  (routing itself never breaks if memory is unavailable).

## Desktop

The desktop app gains a **Memory** tab (`forge/desktop_app/app.py`)
backed by headless methods on `DesktopBackend` (`list_memory`,
`search_memory`, `memory_stats`, `delete_memory`, `correct_memory`,
`memory_provenance`).

## CLI

```
forge memory                     # list recent memories
forge memory search <query>      # relevance-ranked search
forge memory stats               # counts by type/status + redactions
forge memory add --type T "..."  # store an item
forge memory correct <id> "..."  # supersede an item
forge memory delete <id>         # soft-delete an item
```

Common flags: `--db PATH` (default `.forge/memory.db`), `--project ID`
(default current directory name), `--type`, `--limit`, `--json`.

## Security notes

- Memory is project-scoped: one project can never read another's.
- Redaction happens *before* any write; the database only contains
  redacted text and counts.
- Integrations are best-effort and guarded — a memory failure can never
  break the pipeline, routing, or the UI.
- The legacy file-backed `forge.memory.store.MemoryStore` (A37) is
  unchanged and independent.

## Testing

`tests/test_memory_engine.py`, `tests/test_memory_redaction.py`,
`tests/test_memory_integrations.py`, `tests/test_memory_cli.py`, plus
the desktop memory-viewer stub test — covering persistence, retrieval,
ranking, duplication, redaction, deletion, project isolation, flood
control, summarization, correction, provenance, and retention, along
with the planner/context/debugger/reviewer/router and CLI/desktop
surfaces.
