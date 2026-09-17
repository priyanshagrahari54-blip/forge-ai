# Forge Final Integration Contract

This document is the single consolidated contract for the 20 remaining work areas.
It deliberately distinguishes implemented code from runtime resources that require
operator credentials, external workers, GPUs, or deployment accounts.

## Integrated in the repository

1. Worker persistence, liveness, heartbeat and stale-worker reaping.
2. Worker capability/resource admission and execution gateway.
3. Local-by-default execution with an explicit remote boundary.
4. Existing secure remote transports remain fail-closed: allowlists, host-key
   verification, DNS/IP policy, bounded execution, and no shell transport.
5. Persistent task queue, leases, ownership fencing, checkpoints and recovery.
6. Supervisor-driven coding/test/debug/review/security/acceptance loop.
7. Model Fabric routing, model inventory/discovery, verification and failover.
8. Logical 1000-slot specialist agent fleet routed through the configured fabric.
9. Persistent event log and live/replayable AI City event stream.
10. Project-scoped storage, Git integration and generated-file reporting.
11. Background server execution independent of a browser tab.
12. Runtime provider health/truth reporting; configured credentials are not treated
    as proof of reachability.
13. Voice/vision interfaces with simulated defaults and explicit provider states.
14. Real web research path with SSRF-safe URL fetching and provenance labels.
15. Compute engine with bounded local execution and guarded remote backends.
16. Security policy, permissions, approvals, audit and verification gates.
17. G560 thin-client profile: small payloads, slower polling, no heavy local media
    or model execution, remote execution preferred.
18. Deployment/backup/plugin/observability infrastructure already present in the
    server composition and API surface.
19. Unified `/api/v1/readiness` plus `/api/v1/runtime` machine-readable status.
20. Cross-stack integration tests covering worker admission/release, G560 policy,
    and the unified runtime truth contract.

## Runtime-dependent, not falsely marked complete

- A remote worker is only LIVE after registration plus a fresh heartbeat and a
  successful authenticated execution transport.
- OpenAI/Ollama/other external model providers require their real credentials and
  reachable runtime. The catalog/inventory never implies live inference by itself.
- Voice/vision become LIVE only after a real provider is configured and a runtime
  verification succeeds.
- Web research requires a configured search provider or SearXNG endpoint.
- Production deployment requires the operator's hosting account, secrets and
  external infrastructure.
- Large GPU workloads, training clusters and multi-machine execution require real
  compute resources; Forge does not fabricate those resources.

## Truth rule

The runtime snapshot is observational. It does not activate anything and it never
promotes `ARCHITECTURE`, `CONFIGURED`, `SIMULATED`, `BLOCKED`, `ERROR`, or
`UNAVAILABLE` to `LIVE` without runtime evidence.

## Client architecture

The Lenovo G560 is treated as a thin control client. Heavy model inference,
remote execution, builds, media processing and other resource-intensive work are
server/worker responsibilities. This prevents the 2 GB/32-bit client from being
mistaken for the compute cluster.
