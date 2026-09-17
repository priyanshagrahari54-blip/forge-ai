# Forge Capability Reality

Forge reports capability state from runtime evidence rather than from UI labels or adapter count.

## States

- `LIVE` — the capability has runtime evidence that it is available and verified.
- `READY` — the capability path is wired and usable when its required runtime is available.
- `CONFIGURED` — configuration exists, but successful live verification has not been established.
- `ARCHITECTURE` — the software path exists, but a required external runtime/service is not currently proven live.
- `SIMULATED` — the implementation intentionally uses a simulation/test provider.
- `BLOCKED` — the capability was checked but a required dependency was unavailable or unreachable.
- `ERROR` — runtime inspection failed.
- `MISSING` — no supported implementation was found.

## Provider/model links

Forge must not turn a provider adapter, model name, documentation URL, or registry entry into a claim that the model is live. A provider is reported as live only when runtime evidence supports that conclusion.

Likewise, Forge does not imply a Neuralink/brain-computer-interface integration merely because the platform supports voice, vision, computer-use, or external AI providers. There is currently no Neuralink integration in this repository. Any future BCI integration must be implemented as an explicitly identified provider/plugin with its own authentication, permissions, hardware/runtime checks, safety gates, and evidence state.

## External URLs and research

External links may be surfaced as research evidence or provider documentation, but a URL is not itself a capability. Links should retain provenance and should never be presented as a verified runtime connection unless the corresponding provider health check succeeds.

## Client contract

The authenticated `GET /api/v1/capabilities` endpoint is the server-side source for capability truth. Clients should consume that endpoint instead of maintaining independent hard-coded claims about model/provider readiness.
