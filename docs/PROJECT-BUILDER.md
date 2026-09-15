# Forge Project Builder

`ProjectBuilder` is the product-level API for the autonomous software-engineering loop.

## Contract

```text
natural-language requirement
        ↓
real-model preflight
        ↓
Supervisor
        ↓
plan → code → test/debug → review → security → acceptance → git commit
```

The builder does not implement a second planner, router, checkpoint system, or
permission system. It delegates to the existing guarded `Supervisor` so the
same A32/A33 controls remain authoritative.

## CLI

After installing Forge, use:

```bash
forge-build --preflight "Build a FastAPI notes service" --json
forge-build --approve --project notes --root ./notes \
  "Build a FastAPI notes service with SQLite, tests, and a README"
```

`--preflight` returns non-zero when no real coding-capable model is available.
This is intentional: the deterministic/reference engine proves infrastructure
but is not a coding model and must never be presented as one.

## Real model requirement

Project building requires a configured real-capable model through Forge's Model
Fabric / Session-11 inference path. The builder never downloads weights or
silently falls back to the deterministic reference network.

Typical operator workflow:

1. Configure a supported local or remote model/backend.
2. Discover and verify it through Forge's model tooling.
3. Run `forge-build --preflight ...`.
4. Start a build with the desired A32 mode and approval posture.
5. Inspect the structured result and repository diff/commit.

## Embedding API

```python
from forge.project_builder import ProjectBuilder

builder = ProjectBuilder(
    "my-app",
    root="./my-app",
    mode="autonomous",
)

result = builder.build(
    "Build a small web application with tests and documentation",
)

if not result.success:
    raise RuntimeError(result.error)
```

`result.result` contains the bounded Supervisor report, including stages,
gates, timings, model provenance, changed files, and rollback status.
