# Forge AI — Server-First Architecture

Forge is designed so the remote client is a **thin client** and the Forge Server is the **execution authority**. A client device should not need to host the Forge control plane, workers, database, or model runtime.

## Target topology

```text
                    USER
                     │
                     ▼
          ┌─────────────────────┐
          │ Thin Client         │
          │ G560 / browser      │
          │ UI + input + status │
          └──────────┬──────────┘
                     │ authenticated HTTP
                     ▼
          ┌─────────────────────┐
          │ FORGE SERVER        │
          │                     │
          │ API Gateway         │
          │ Authentication      │
          │ ControlPlane        │
          │ Task Queue          │
          │ Workers             │
          │ Agents              │
          │ Model Fabric        │
          │ Tool Runtime        │
          │ Policy/Audit        │
          │ Verification        │
          │ Checkpoints         │
          │ Git/Repository ops  │
          │ Persistent SQLite   │
          └──────────┬──────────┘
                     │
              model/provider APIs
                     │
                     ▼
              External AI Models
```

## Client responsibilities

The thin client is responsible for presentation and interaction only:

- render Forge City / mission UI;
- submit typed task requirements;
- display task status, progress, events, logs and results;
- request pause/resume/cancel/retry where authorized;
- display and submit operator approval decisions;
- reconnect and recover server state after a disconnect.

The thin client must **not** become a second execution plane in server mode. It must not run Forge workers, the Forge database, repository mutations, or local model inference merely because the browser is open.

## Server responsibilities

The Forge Server is authoritative for all persistent execution:

- task creation and lifecycle state;
- queueing, leases, retry and restart recovery;
- ControlPlane/Supervisor orchestration;
- agent selection and execution;
- Model Fabric routing;
- Tool Runtime and ChangeApplier mutations;
- A33 permission/policy checks, approvals and audit events;
- testing, verification and acceptance gates;
- checkpoints and rollback state;
- repository/Git operations;
- persistent events, logs, notifications and results.

A disconnected client must not cancel or terminate server work merely by closing its tab. Work continues until the server reaches a terminal state, the task is explicitly controlled, or the server itself stops.

## No public website requirement

Forge does not require a public hosting URL. The browser needs a network endpoint, but that endpoint may be:

- localhost when the client and server are on the same machine;
- a private LAN address;
- a private VPN address such as Tailscale.

Port `8300` is the default Forge Web/API port in direct-server mode. Do not expose it directly to the public Internet without an intentionally configured HTTPS/authentication boundary.

## Server modes

### Direct Forge Server

Run the standalone server with the server package/CLI or the Docker deployment described in `DIRECT_SERVER.md`. Persistent state lives on the server, not the client.

### Thin-client connection

Desktop/CLI server mode uses the Forge Server client protocol and challenge/response authentication. The client sends typed task data and receives server state; it does not receive a remote-shell interface.

## Resource boundary

The architecture is intentionally asymmetric:

| Resource | Thin client | Forge Server |
|---|---|---|
| UI rendering | yes | serves data/UI |
| Task orchestration | no | yes |
| Background workers | no | yes |
| Persistent database | no | yes |
| Repository mutations | no | yes |
| Test/build execution | no | yes |
| Checkpoints/rollback | no | yes |
| Policy/approval authority | display/submit only | yes |
| Model inference | no in server mode | yes / model provider |
| Long-running mission state | cached view only | authoritative |

## Operational rule

When a Forge client is connected to a Forge Server, **the server is the source of truth**. The client may be restarted, disconnected, upgraded, or closed without transferring ownership of execution to the client.
