# Forge AI repair progress

## Implemented and tested

- Repository intelligence and bounded context selection remain the source of agent context.
- `ModelRouter` now records and scores capability, complexity, context, availability, reliability/failure rate, latency, cost, and free/local preference. Providers are optional and injectable.
- `CoderAgent` consumes a routed provider response, validates structured model output, and applies files through the permissioned runtime. Normal operation does not accept caller-authored changes.
- `DebuggerAgent` and `TestDebugLoop` pass real failure output to a model, apply proposed repairs, and rerun tests with a hard retry bound.
- `CheckpointManager` snapshots file contents and restores exact pre-change contents without `git reset --hard`; unrelated files are preserved.
- Verification runs tests, compilation/build, lint/type-equivalent compilation, secret scanning, and independent diff review. Acceptance uses actual results.
- Git commits stage an explicit validated file list and reject `.forge` runtime state.
- A26-A30 self-development uses the model-driven coder path when no compatibility modifier is supplied, checkpoints before editing, benchmarks test/build latency and outcomes, verifies, commits or rolls back, and records reproducible history.
- `tests/test_autonomous_e2e.py` creates an isolated Git repository, uses the model contract to add CSV export and tests, runs gates, and checks explicit staging. It also verifies exact rollback.

## Deliberate limitations

- The local provider is a safe offline fallback and refuses to synthesize arbitrary source. Autonomous production coding requires an available Ollama or optional API provider.
- Approval and permission gates are preserved. This repository does not silently enable write or push permissions for an external project.
- No feature is marked complete solely because metadata exists; all claims above are covered by executable tests.
