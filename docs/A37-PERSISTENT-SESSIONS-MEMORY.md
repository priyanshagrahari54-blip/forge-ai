# A37 — Persistent Sessions + Memory

A37 makes the cockpit's state durable and remembered. Sessions, session
tokens, active-task bindings, and memory already lived in one SQLite
database; A37 adds the *remembered* layer on top: session-scoped memory
(notes, facts, summaries), project-wide durable knowledge, run-outcome
summaries, and the policy-gated API + cockpit surface for all of it.
Everything survives a backend restart.

```text
COCKPIT / AGENT
 ↓
MEMORY API (/api/v1/memory/*)          every access gated by
 ↓                                      Resource.MEMORY read/write/delete
A33 POLICY + APPROVALS                 (single-use tokens, session-bound)
 ↓
SESSION MEMORY (SQLite, bounded,       notes / facts / summaries per
session-scoped)                         session
PROJECT MEMORY (MemoryStore,           facts / decisions / run summaries
path-safe files under .forge/memory)    per project
```

## What A37 adds

| Area | Location | Notes |
|---|---|---|
| Memory resource | `forge/security/policy.py` | `Resource.MEMORY` with `read`/`write`/`delete` |
| Session memory | `forge/control/memory.py` | SQLite-backed, session-scoped, bounded (500 entries / 20 KB per entry / 1 MB per session), FIFO pruning |
| Project memory wiring | `forge/control/control_plane.py` | Lazy per-project `MemoryStore` under `.forge/memory` |
| Run memory | `forge/control/control_plane.py` | Bounded run-outcome summaries (last 50) on run finish |
| API | `forge/api/routes_memory.py`, `schemas.py` | `/api/v1/memory*`, rate-limited mutations |
| Cockpit | `forge/cockpit/web/` | Memory view: session notes, project keys, approvals |
| Tests | `tests/test_a37_*.py` | 30 tests across 5 suites |

## Memory model

- **Session memory** — remembered state of one cockpit session: `note`,
  `fact`, `summary` entries with source and timestamp. Strictly
  session-scoped: a session can only read/write/delete its own entries
  (cross-session access is NOT_FOUND, never 403). Bounded per entry and
  per session with FIFO pruning, so memory can never grow unbounded.
- **Project memory** — durable project knowledge in the existing
  path-safe `MemoryStore` (rejects traversal; the API additionally caps
  keys at 256 chars and content at 20 KB). Keys like `facts/deploy`.
- **Run summaries** — when a run finishes, the control plane records a
  bounded summary (`status`, `requirement`, `model`, `provider`,
  `attempts`, `rollback`, `finished_at`) into project memory under
  `runs/{run_id}`, keeping the most recent 50. This is system
  observability (like events/audit), never secrets; disable with
  `ControlConfig(memory_record_runs=False)`.

## Policy gating (the A33 gate, never bypassed)

Every memory access evaluates `Resource.MEMORY` against the permission
policy with the request identity `forge-memory` (so a human session
actor can decide its approvals; the store enforces approver ≠ agent):

- `ALLOW` — proceeds.
- `DENY` — blocked with the policy reason.
- `REQUIRE_APPROVAL` — files a session-bound approval; approving mints
  a single-use token that the resubmitted request redeems. Stale/spent
  tokens fail closed into a fresh, decidable approval.

Read-only overviews are policy-filtered: `memory_overview` returns only
the session entries and project keys the policy currently allows
reading. Memory never bypasses permissions, and entries are served
redacted at the boundary.

## API

| Method | Effect |
|---|---|
| `GET /api/v1/memory` | Policy-filtered overview: session entries (metadata) + project keys |
| `POST /api/v1/memory` | Add a session entry `{kind, content, approval_id}` (rate-limited) |
| `GET /api/v1/memory/entries/{id}` | Full session entry (redacted) |
| `POST /api/v1/memory/delete` | Delete a session entry `{entry_id, approval_id}` (rate-limited) |
| `GET /api/v1/memory/project` | Project keys the policy allows |
| `POST /api/v1/memory/project/save` | Save project memory `{key, content, approval_id}` (rate-limited) |
| `GET /api/v1/memory/project/get?key=` | Load a project memory key |
| `GET /api/v1/memory/approvals` | Session-visible pending memory approvals |
| `POST /api/v1/memory/approvals/{id}/approve` | Decide + mint single-use token (rate-limited) |
| `POST /api/v1/memory/approvals/{id}/deny` | Decide deny (rate-limited) |

Kinds are validated before the policy gate (`note`/`fact`/`summary`
only); content is bounded (1–20 000 bytes); keys are validated
(1–256 chars, no traversal). All mutations are rate-limited, CSRF-gated,
and audited (`resource="memory"`).

## Persistence

The cockpit database is the single durable source: sessions, session
tokens, active-task bindings, run records, and session memory all live
in SQLite, and project memory lives in the project root. A brand-new
`ControlPlane` over the same database resumes everything — pinned by
`tests/test_a37_persistence.py` (session + token, session/project
memory, runs, active-task binding, expiry still enforced after
restart). Honest limitation: in-memory approval tokens and the A35
desktop/voice simulated providers are runtime state and do not survive
restarts (approvals are short-lived by design — 5-minute TTL).

## Cockpit

The Memory view (`#/memory`) shows session memory with kind/source/
timestamp, an add-note form, show/forget actions; project memory keys
with load and save; and pending memory approvals with approve/deny that
carry the minted token into the resubmission. All UI contracts hold:
same-origin `api()` only, no storage, no credentials, CSP-clean,
navigation-only palette ("Go to Memory").

## Testing

- `tests/test_a37_memory_store.py` — session memory bounds, scoping,
  FIFO pruning, durability.
- `tests/test_a37_memory_plane.py` — policy gating (DENY/ALLOW/
  REQUIRE_APPROVAL + token round trip), cross-session isolation, path
  safety, run-summary recording and retention, input validation, the
  MEMORY policy vocabulary.
- `tests/test_a37_persistence.py` — full restart persistence including
  expiry.
- `tests/test_a37_api.py` — API integration incl. 401/400/404
  boundaries and audit.
- `tests/test_a37_ui.py` — cockpit memory view contracts + live
  endpoints.

A37 result: **30 new tests; full suite 1105 passed, 2 skipped.**
