# FORGE AI — OPERATIONS
## Deployment
Commit → CI → deploy → health → smoke test → runtime verification.

## Monitoring
Track API health, task queue depth, worker utilization, provider health, model verification, latency, errors, retries and resource usage.

## Recovery
Workers use leases/heartbeats. Restart recovery must identify expired work and safely requeue/reconcile.

## Provider operations
Periodically verify configured providers without making expensive inference calls unnecessarily; perform bounded inference probes when promoting readiness.

## Rollback
Keep known-good commit/deployment reference. Roll back application and related schema/config changes coherently.

## Incidents
Detect → classify → contain → diagnose → repair → test → deploy → verify → document.
