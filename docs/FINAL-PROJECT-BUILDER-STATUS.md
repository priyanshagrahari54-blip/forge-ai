# Forge Project Builder — Finalization

## Product target

Forge's first complete product milestone is a guarded autonomous software-engineering loop that can take a natural-language requirement and, when a verified coding-capable model is configured, plan, inspect a repository, modify files, run tests, repair failures, review, security-check, accept, checkpoint, and commit the resulting project.

## Current architecture

```text
User / Forge Cockpit / CLI
        |
        v
ProjectBuilder / Server Queue
        |
        v
ExecutionContext + Attempt Fence + A33 Policy
        |
        v
Supervisor
   |    |    |    |    |
Plan  Code Test Review Security
        |      |
        v      v
   Inference Fabric
        |
        v
Routing -> Verification -> Residency -> Backend
        |
        +--> Ollama (local, configured by operator)
        +--> OpenAI-compatible remote provider (optional)
        +--> other verified backend adapters

Successful candidate
        |
        v
Acceptance -> explicit files -> Git commit
        |
        v
Durable task/event/result state in SQLite
```

## Non-negotiable truth

Forge does not ship a production coding model in this repository. The first-party reference engine exists only to prove the inference infrastructure and is deliberately excluded from project-builder capability routing. A real coding model/backend must be configured and verified before a project build can succeed.

## Definition of done for the first milestone

- One product API: `forge.project_builder.ProjectBuilder`.
- One product CLI entrypoint: `forge-build`.
- Existing Supervisor remains the only engineering transaction.
- Existing A32/A33 permissions, checkpointing, inference routing, and acceptance gates remain authoritative.
- Background work is independent from the client connection.
- Attempt identity/fencing is propagated into inference and terminal publication.
- Real model provenance is preserved; deterministic fallback cannot masquerade as neural output.
- Tests/debugging are bounded and acceptance requires the complete verification set.
- Git commits occur only through the guarded acceptance path.
- The G560 remains a thin client and does not load local models.

## Operator path

```text
configure a real backend/model
        -> discover
        -> verify
        -> forge-build --preflight ...
        -> forge-build ...
        -> inspect result / events / git commit
```

## Remaining external dependency

The repository can provide the software-building platform, but the capability level of generated software is bounded by the real coding model the operator configures. No repository-only change can honestly replace a missing capable model with the same quality.
