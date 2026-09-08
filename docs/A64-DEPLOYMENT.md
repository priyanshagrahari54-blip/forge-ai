# A64 — Deployment

Validated, verifiable, rollback-capable deployments.

## What A64 adds

- `forge/deployment/manager.py` — deployment lifecycle
  (created → built → deployed → rolled_back) persisted in the plane
  database:
  - `create`: validated name/version, ≤8 deployments per project;
  - `build`: bounded project snapshot (≤1000 files, ≤50 MB) into a
    real zip artifact with a sha256 manifest per file; the last 3
    artifact versions are kept;
  - `deploy`: extraction into a target directory **outside** the
    source project; every file's hash is verified against the
    manifest before writing; unknown non-empty targets are refused;
  - `rollback`: restores the previous artifact version into the
    same target, one step back, honestly refusing when no previous
    version exists.
- Control plane `deployment_create/build/deploy/rollback/list/get`
  (audited); API at `/api/v1/deployments*`.

## Security notes

- Deployments are local staging, honestly labeled: no network, no
  secrets exported beyond project files (the walk skips forge
  metadata dirs), targets outside the project only, hash-verified
  writes, no overwrite of unknown directories.

## Testing

`tests/test_a64_deployment.py` (5): create validation + caps,
real artifact + manifest contents, verified extraction outside the
project with refusal cases, rollback restoring the previous version
with refusal past the oldest, API flow.

A64 result: **5 new tests; full suite 1348 passed, 2 skipped** (A63
baseline: 1343 passed, 2 skipped).
