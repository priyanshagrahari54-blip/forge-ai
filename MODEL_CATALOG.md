# FORGE AI — MODEL CATALOG
The catalog is generated from configured provider discovery where possible. Never hard-code imaginary models.

## Status vocabulary
CATALOGUED, REGISTERED, CONFIGURED, DISCOVERED, REACHABLE, VERIFIED, LIVE, BLOCKED, ERROR.

## Required metadata
provider, model_id, capabilities, context_length, modalities, reasoning/tool support where known, endpoint class, verification timestamp, latency, reliability, cost metadata and limitations.

## Verification
A model becomes VERIFIED only after an actual bounded inference probe succeeds. Discovery alone never promotes a model to LIVE.

## Routing
Only eligible verified/live models may serve production tasks unless the caller explicitly requests a non-production diagnostic path.
