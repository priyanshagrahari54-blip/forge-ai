# A49 — Agent Creation

Agents can now be defined at runtime — as honest specifications, not
capability claims.

## What A49 adds

- `forge/agents/factory.py` — `AgentFactory` creates validated
  `AgentDefinition`s: name/role regexes, capabilities restricted to
  the canonical A31 19-capability vocabulary, deduplicated and
  bounded (12 caps, 40 agents/session).
- **Honest `real` flag**: a definition is `real=True` only when it
  binds the registered executor that actually exists for its role
  (e.g. role `coding` → the real `coder` executor); otherwise it is
  labeled as a specification without a backing executor and cannot
  run tasks. Role changes invalidate bindings.
- **Creation grants nothing**: defined agents never appear in the
  built-in agent catalog and gain no permission power; execution (a
  later stage) will remain behind the same A33 gates.
- Control plane CRUD (`agent_create/update/delete/definitions`,
  audited under `agents`); API: `POST /api/v1/agents`,
  `GET /api/v1/agents/defined`, `PATCH/DELETE /api/v1/agents/{name}`.
- Cockpit Agent Builder view (create form + definition list, real
  flag shown via each definition's note).

## Testing

`tests/test_a49_agent_creation.py` (7): validation (names/roles/
vocabulary/dedup), honest real-flag and binding invalidation, plane
CRUD with audit, no-power guarantee (catalog unchanged), API
boundaries, cockpit contracts.

A49 result: **7 new tests; full suite 1270 passed, 2 skipped** (A48
baseline: 1263 passed, 2 skipped).
