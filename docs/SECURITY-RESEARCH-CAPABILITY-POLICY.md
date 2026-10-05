# Forge — High-Capability Security & Reverse-Engineering Policy

Forge is designed to support legitimate, authorized security research and reverse engineering without artificially weakening technical capability.

## Supported domains
- authorized penetration testing
- CTF and isolated security labs
- defensive vulnerability research
- source and binary analysis
- protocol/interoperability analysis
- malware analysis in an isolated sandbox
- OS/kernel security research
- application and web security testing
- dependency and supply-chain auditing
- secure exploit reproduction in owned/authorized targets

## Execution model
HIGH-RISK capabilities run through explicit authorization, scoped target identity, sandboxing, least privilege, network policy, secrets isolation, audit logging and rollback.

## Agent policy
Agents are dynamically created/selected according to the current task graph. There is no fixed maximum specialist count in the architecture. The scheduler must still enforce resource, permission and concurrency limits.

## Reverse-engineering policy
Forge may analyze user-owned, licensed, open-source, or otherwise authorized software and hardware. License/access restrictions remain part of the requirement contract.

## Security principle
“High capability” does not mean “unbounded authority.” Forge should preserve the maximum technically useful capability while keeping execution scoped to the authorization and environment supplied for the task.

## External ecosystem
Security-agent harnesses, sandboxes, reverse-engineering tools, debuggers, disassemblers, fuzzers and analysis frameworks should be discovered and evaluated through the capability-adoption pipeline before integration.
