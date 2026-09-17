# Forge Execution Fabric

This document defines the combined execution path now exposed by Forge.

```text
Task / orchestration
      |
      v
Execution requirements
      |
      +---- local ----> bounded local backend
      |
      +---- remote ---> worker admission
                            |
                            v
                     authenticated transport
                            |
                            v
                     backend-enforced policy
                            |
                            v
                 result / checkpoint / events
                            |
                            v
                    release worker + audit
```

## What is implemented

- Worker capability/resource admission with fail-closed liveness checks.
- Worker heartbeat reaping and durable worker metadata storage.
- A single `ExecutionGateway` boundary for local versus explicitly remote work.
- Declarative `SandboxPolicy` that clearly distinguishes policy from actual OS isolation.
- Existing persistent task leases, heartbeats, stale-lease recovery and checkpoints remain the source of truth for task ownership/recovery.
- Existing remote compute security remains mandatory: explicit destination allowlists, host-key verification for SSH, destination-IP classification, bounded execution, no shell-based user-code transport.
- Evidence-based capability/readiness reporting.
- A low-resource thin-client profile for machines such as the Lenovo G560: the browser/client remains lightweight while server-side execution does the heavy work.

## What still requires external runtime resources

The architecture can be complete without pretending these resources exist. Live operation still depends on the operator's environment:

- A remote worker must actually be registered, reachable and heartbeating.
- An SSH/Colab-compatible/Modal backend must be explicitly configured and pass its runtime checks.
- External model providers require valid credentials and successful verification.
- Voice/vision/web research become live only after their real provider integrations pass verification.
- Production deployment is live only after a real target is configured and verified.

These are runtime facts, not missing UI placeholders. The readiness layer deliberately reports them separately.

## Thin-client rule

A G560-class endpoint should not be treated as the compute cluster. Its role is cockpit, control, monitoring and lightweight local operations. Heavy model inference, large builds, GPU work, media processing and long-running autonomous tasks should be routed to configured server/worker infrastructure.
