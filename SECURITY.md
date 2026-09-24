# FORGE AI — SECURITY SPECIFICATION
## Threats
Prompt injection, malicious repositories/files, SSRF, DNS rebinding, command injection, path traversal, secret leakage, unsafe tool arguments, privilege escalation, cross-project leakage and poisoned research.

## Controls
Authentication; authorization; least privilege; task-scoped credentials; secret redaction; URL validation; network policy; sandboxing; resource limits; timeouts; audit logs; approval gates; output validation; provenance.

## Trust boundary
User input, model output, web content, uploaded files, repositories and tool output are untrusted until validated.

## High-risk actions
Destructive filesystem changes, credential use, external transactions, deployment and privilege changes require explicit policy/approval.

## Testing
Negative tests for every threat category; regression tests for fixed vulnerabilities; secret-scanning in CI; dependency/security checks.

## Incident response
Detect → contain → revoke → preserve audit evidence → rollback → patch → regression test → redeploy → verify.
