# A48 — Compute

Managed computation: code cells that really execute, with honest
resource accounting and the same permission posture as every other
controlled action. Supports both local Python execution and remote
backends (a Colab-compatible kernel proxy, SSH, Modal) when configured
by the operator.

## What A48 adds

- `forge/compute/engine.py` — `ComputeEngine` executes cells in a
  fresh `python -I -c` subprocess against the project working
  directory. Status (`succeeded`/`failed`/`timeout`) comes from real
  exit codes and real timeouts; output is captured and truncated to
  4000 chars with an explicit `output_truncated` flag. Quotas
  (cells + total seconds + per-cell timeout) are enforced **before**
  execution and refusals leave no side effects. Backend is labeled
  `local-python` by default.
- `forge/compute/remote.py` — **Real remote compute backends**:
  - **Kernel proxy** (`FORGE_COLAB_URL`): execute code against an
    operator-provided proxy speaking the Colab-style JSON kernel
    protocol (`POST /execute`). Forge does not bundle or claim a
    Google Colab / Google-account integration; the operator points
    this at the proxy endpoint they control.
  - **SSH Remote** (`FORGE_COMPUTE_SSH_HOST`): execute code on a
    remote server via SSH (stdin-only transport, mandatory
    host-key verification, explicit destination allowlist).
  - **Modal** (`MODAL_TOKEN_ID`): execute code inside a Modal
    sandbox (arg-list transport).
  - All remote backends use the same permission gate, quotas, and
    bounded output. Results carry the real backend name.
- **Destination-IP policy** (applies to SSH and kernel-proxy URLs
  alike): the configured hostname is resolved and **every** returned
  address is classified (public / loopback / private / link-local /
  cloud-metadata / multicast / reserved / …) **before any connection
  is attempted**. Any non-public address refuses the destination —
  fail-closed on unresolvable/unknown hostnames — unless the operator
  explicitly opts in (see the table below; a hostname on the SSH
  allowlist never bypasses this policy). Cloud-metadata endpoints
  (`169.254.169.254`) are refused even with an opt-in.
- Control plane `compute_execute()` — gated by TERMINAL/execute
  policy exactly like terminal commands (ALLOW rules must pin the
  concrete executable and exact args — the existing A33 hardening is
  unchanged; REQUIRE_APPROVAL files a task-scoped approval and only
  a redeemed single-use token releases the run; DENY fails closed).
  Every run is audited with real status/timing.
- `compute_status/compute_history` + session-isolated compute
  approvals; API `/api/v1/compute/*` (execute rate-limited, status,
  history, approvals, approve/deny).
- Cockpit Compute view (cell input, result, quota, history) with the
  usual UI contracts.
- `ControlConfig` gains `compute_max_cells` (20),
  `compute_max_seconds` (300), `compute_cell_timeout` (30).

## Configuration

| Variable | Effect |
|---|---|
| `FORGE_COLAB_URL` | Operator-provided kernel-proxy URL (Colab-style `POST /execute` protocol). Scheme, credentials, port, hostname, and resolved addresses are validated before connect. |
| `FORGE_COLAB_ALLOW_HTTP` | `1` permits an explicit `http://` proxy URL (default https only). |
| `FORGE_COLAB_ALLOW_LOCALHOST` | `1` permits loopback/link-local proxy endpoints (explicit operator opt-in; the proxy runs on this machine). |
| `FORGE_COLAB_ALLOW_PRIVATE` | `1` permits proxy endpoints on private networks (explicit operator opt-in; still never the cloud-metadata address). |
| `FORGE_COMPUTE_SSH_HOST` | SSH host for remote execution (e.g., `user@host`) |
| `FORGE_COMPUTE_SSH_ALLOWLIST` | Comma-separated destinations allowed (fail-closed when absent) |
| `FORGE_COMPUTE_SSH_ALLOW_PRIVATE` | `1` permits SSH destinations that resolve to non-public addresses — the explicit operator-controlled private-network exception (never the never-routed set: cloud metadata, multicast, documentation/benchmark/reserved ranges) |
| `FORGE_COMPUTE_SSH_KNOWN_HOSTS` / `FORGE_COMPUTE_SSH_IDENTITY` / `FORGE_COMPUTE_SSH_PORT` | Strict host-key file, optional identity file, port |
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | Modal serverless credentials |

- Compute inherits the terminal permission model; nothing executes
  without a policy decision, and approvals bind to the session/task.
- Timeouts bound runaway cells; quotas bound session usage; output
  is bounded so cells cannot flood memory.

## Testing

`tests/test_a48_compute.py` (10): real success/failure/timeout
execution, quota enforcement before execution, output truncation and
bounded history, gated plane runs with audit, DENY fail-closed,
approval round trip with spent-token refile, API boundaries, cockpit
contracts.

`tests/test_a48_compute_security.py` — Level 1 fail-closed config
parsing; Level 2 transport integrity (code only ever over stdin,
never argv/shell; mandatory host-key verification; allowlist);
Level 3 destination-IP policy (every resolved address classified;
loopback/private/link-local/metadata refused without an explicit
operator opt-in; unresolvable hosts fail closed; allowlist alone
never bypasses the policy; opt-in still routes through the strict
transport); Level 4 kernel-proxy URL policy (scheme/credentials/
port/local-endpoint rules + resolved-address safety, deterministic
offline behavior for literals).
