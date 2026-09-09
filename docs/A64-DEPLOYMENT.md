# A64 — Deployment

Validated, verifiable, rollback-capable deployments with real
production backends.

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
- `forge/deployment/production.py` — **Real production backends**:
  - **Docker**: Build and push container images to a registry
    (`FORGE_DOCKER_REGISTRY`).
  - **Kubernetes**: Apply manifests to K8s clusters
    (`FORGE_K8S_NAMESPACE`).
  - **SSH/rsync**: Deploy to remote servers via rsync
    (`FORGE_DEPLOY_SSH_HOST`, `FORGE_DEPLOY_SSH_PATH`).
  - **Fly.io**: Deploy via the Fly CLI (`FLY_API_TOKEN`).
  - All production backends build on the same manifest/artifact
    system and require explicit configuration.
- Control plane `deployment_create/build/deploy/rollback/list/get`
  + **`deployment_backends`** + **`deployment_docker_build`** +
  **`deployment_ssh_deploy`** (audited); API at
  `/api/v1/deployments*`.

## Configuration

| Variable | Effect |
|---|---|
| `FORGE_DOCKER_REGISTRY` | Docker registry for image push |
| `FORGE_DOCKER_USERNAME` | Docker registry username |
| `FORGE_K8S_NAMESPACE` | Kubernetes namespace (default: `default`) |
| `FORGE_DEPLOY_SSH_HOST` | SSH host for rsync deploy |
| `FORGE_DEPLOY_SSH_PATH` | Remote path for rsync deploy |
| `FLY_API_TOKEN` | Fly.io API token |

## Security notes

- Local staging is the default: no network, no secrets exported
  beyond project files (the walk skips forge metadata dirs), targets
  outside the project only, hash-verified writes, no overwrite of
  unknown directories.
- Production backends require explicit configuration and are
  permission-gated. API keys are read from environment variables.

## Testing

`tests/test_a64_deployment.py` (5): create validation + caps,
real artifact + manifest contents, verified extraction outside the
project with refusal cases, rollback restoring the previous version
with refusal past the oldest, API flow.

A64 result: **5 new tests; full suite 1348 passed, 2 skipped** (A63
baseline: 1343 passed, 2 skipped).
