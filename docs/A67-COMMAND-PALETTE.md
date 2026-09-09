# A67 — Command Palette

One canonical palette: server catalog, client navigation.

## What A67 adds

- `forge/cockpit_palette.py` — the canonical cockpit command
  palette catalog: 22 view entries plus safe quick actions (create
  task, view approvals, refresh). Targets are hash routes only, so
  palette navigation carries no execution power; the catalog
  validates itself (defense in depth).
- API `GET /api/v1/commands/palette` (authenticated read); the
  cockpit palette (Ctrl+K / Cmd+K) now syncs with the server
  catalog on open — merging only entries whose targets exist in
  the client's route table, and falling back to the local list
  offline.

## Security notes

- Navigation-only: palette entries can move the user between
  views and focus the new-task input; they can never run anything.

## Testing

`tests/test_a67_command_palette.py` (5): catalog coverage and
shape, every server view target exists in the client ROUTES table
(parsed from app.js), the client fetches the endpoint and merges
hash-route-only targets, catalog validation refuses bad entries,
API flow with auth required.

A67 result: **5 new tests; full suite 1363 passed, 2 skipped** (A66
baseline: 1358 passed, 2 skipped).
