# A64 — Deployment

Validated, verifiable deployments with real production backends.
Rollback capability is exactly what the code implements: automatic
rollback for the local staging deployment; for remote SSH/rsync
targets there is no automatic restore — recovery means re-deploying
the previous artifact version through the same validated pipeline
(see Security notes). No broader rollback is claimed.

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
    (`FORGE_DEPLOY_SSH_HOST`, `FORGE_DEPLOY_SSH_PATH`). Transfer is
    followed by remote SHA-256 verification of deployed files; a
    failed verification fails the deploy (never a silent success).
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
| `FORGE_DEPLOY_SSH_HOST` | SSH deploy destination (e.g., `deploy@host[:port]`) |
| `FORGE_DEPLOY_SSH_USER` | SSH user when not embedded in the host spec |
| `FORGE_DEPLOY_SSH_PATH` | Remote absolute path for rsync deploy |
| `FORGE_DEPLOY_SSH_ALLOWLIST` | Comma-separated destinations allowed (fail-closed when absent) |
| `FORGE_DEPLOY_SSH_ALLOW_PRIVATE` | `1` permits deploy destinations that resolve to non-public addresses — the explicit operator-controlled private-network exception (never the never-routed set: cloud metadata, multicast, documentation/benchmark/reserved ranges) |
| `FORGE_DEPLOY_SSH_PORT` / `FORGE_DEPLOY_SSH_IDENTITY` / `FORGE_DEPLOY_SSH_KNOWN_HOSTS` | Optional port, identity file, strict known_hosts file |
| `FLY_API_TOKEN` | Fly.io API token |

## Security notes

- Local staging is the default: no network, no secrets exported
  beyond project files (the walk skips forge metadata dirs), targets
  outside the project only, hash-verified writes, no overwrite of
  unknown directories.
- SSH/rsync destinations must appear on
  `FORGE_DEPLOY_SSH_ALLOWLIST` — deployment is refused otherwise
  (fail-closed). SSH options are strict: `StrictHostKeyChecking=yes`
  against the configured known_hosts, `BatchMode=yes`, no password
  prompts, no `StrictHostKeyChecking=no`, argv lists only (no shell).
- Destination-IP policy (shared with remote compute): the deploy
  hostname is resolved and EVERY returned address is classified
  before rsync may start. Any non-public address refuses the deploy
  unless the operator explicitly set
  `FORGE_DEPLOY_SSH_ALLOW_PRIVATE=1`; unresolvable/unknown hostnames
  fail closed; the cloud-metadata endpoint and the never-routed set
  are refused even with the opt-in. A hostname on the allowlist never
  bypasses this policy.
- `rsync --delete` NEVER runs without a validated, destination-bound
  approval token: the control plane only sets `delete_approved=True`
  after `enforce_with_token`, and the deployer refuses `--delete`
  without it — an agent cannot delete remotely by flipping an
  argument.
- Production backends require explicit configuration and are
  permission-gated; API keys are read from environment variables.

## Testing

`tests/test_a64_deployment.py`: create validation + caps, real
artifact + manifest contents, verified extraction outside the project
with refusal cases, rollback restoring the previous version with
refusal past the oldest, API flow.

`tests/test_a64_deployment_security.py`: Level 1 fail-closed
destination parsing + allowlist; Level 2 strict rsync argv,
`--delete` approval gating, honest post-deploy verification failures;
Level 3 destination-IP policy — private/unresolvable/metadata
destinations refused (allowlist alone never bypasses), operator
opt-in reaches the transport, literal public destinations allowed.
