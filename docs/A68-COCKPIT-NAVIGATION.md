# A68 — Cockpit Navigation

Canonical keyboard navigation with a help overlay.

## What A68 adds

- `forge/cockpit_shortcuts.py` — the canonical shortcut catalog:
  8 prefix chords (`g d`, `g t`, `g p`, `g m`, `g a`, `g v`,
  `g s`, `g b`) plus immediate keys (`ctrl+k` palette, `?` help,
  `n` new-task focus, `escape`). Chord targets must be real
  cockpit views; the catalog audits itself (pattern, uniqueness,
  kind, targets).
- API `GET /api/v1/commands/shortcuts` (authenticated read). The
  cockpit wires the chords and keys (ignoring typing contexts),
  renders a `?` help overlay from the server catalog with a local
  fallback, and stays class-toggling only.

## Security notes

- Navigation-only by contract and by implementation: shortcuts
  change views or toggle UI overlays; they can never execute
  anything.

## Testing

`tests/test_a68_cockpit_navigation.py` (5): catalog shape +
uniqueness, chord targets are real views, validation refusals,
cockpit wiring (js/html/css static checks incl. the no-inline-
style invariant), API flow with auth required.

A68 result: **5 new tests; full suite 1368 passed, 2 skipped** (A67
baseline: 1363 passed, 2 skipped).
