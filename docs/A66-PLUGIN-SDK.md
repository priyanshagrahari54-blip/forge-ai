# A66 — Plugin SDK

Validated declarations with honest capability bindings.

## What A66 adds

- `forge/plugins/manifest.py` — strict manifest validation
  (format/version, name/version/kind/entrypoint patterns,
  canonical-vocabulary capabilities, bounded counts and sizes).
  Validation never imports or executes anything.
- `forge/plugins/registry.py` — session-bounded declarative
  registry (≤16 plugins) with `bind_capabilities`: a plugin's
  declared capabilities are checked against what is actually
  registered (agent executors, model providers) — declarations
  alone never make a capability real, and unbound capabilities are
  reported honestly.
- Control plane `plugin_install/list/status/remove` (audited); API
  at `/api/v1/plugins*`.

## Security notes

- This SDK is the honest slice of the plugin contract: identity +
  manifest + discovery + capability binding. Loading foreign code
  is deliberately out of scope, so installation cannot smuggle
  execution power into the plane.

## Testing

`tests/test_a66_plugin_sdk.py` (5): strict manifest validation
across ten malformed payloads, install/list/remove, honest binding
(real vs unbound capabilities, no catalog mutation), session
isolation + the 16-plugin cap, API flow.

A66 result: **5 new tests; full suite 1358 passed, 2 skipped** (A65
baseline: 1353 passed, 2 skipped).
